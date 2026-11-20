from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.chat import ChatBusy, ChatExpired, ChatRequest, Conversations
from app.models import Answer, Selection, SitemapEntry


def no_evidence(question):
    if question.product is None:
        return Answer(status="clarification_required", answer="Car, home or business?")
    return Answer(status="abstained", answer="No evidence found.")


def test_clarification_reply_restores_original_question():
    chats, requests = Conversations(), []
    def respond(question):
        requests.append(question)
        return no_evidence(question)
    first = chats.reply(ChatRequest(message="Can I pay monthly or annually?"), respond)
    second = chats.reply(ChatRequest(message="Car insurance", session_id=first.session_id), respond)
    assert first.status == "clarification_required"
    assert requests[-1].question == "Can I pay monthly or annually?"
    assert requests[-1].product == "car"
    assert second.product == "car" and second.session_id == first.session_id
    chats.reply(ChatRequest(message="Can I make a claim?", session_id=first.session_id), respond)
    assert requests[-1].product == "car"


def test_followup_context_uses_user_question_not_assistant_facts():
    chats, requests = Conversations(), []
    def respond(question):
        requests.append(question)
        return Answer(status="answered", answer="DO NOT USE THIS GENERATED TEXT AS KNOWLEDGE.")
    first = chats.reply(ChatRequest(message="Does car insurance cover damage?"), respond)
    chats.reply(ChatRequest(message="What about theft?", session_id=first.session_id), respond)
    assert requests[-1].product == "car"
    assert "Does car insurance cover damage?" in requests[-1].question
    assert "What about theft?" in requests[-1].question
    assert "GENERATED TEXT" not in requests[-1].question


def test_topic_switch_and_separate_chats_do_not_leak_context():
    chats, requests = Conversations(), []
    def respond(question):
        requests.append(question)
        return no_evidence(question)
    first = chats.reply(ChatRequest(message="Does car insurance cover damage?"), respond)
    switched = chats.reply(ChatRequest(message="Does home insurance cover storms?", session_id=first.session_id), respond)
    assert switched.product == "home"
    assert requests[-1].question == "Does home insurance cover storms?"
    independent = chats.reply(ChatRequest(message="What is covered?"), respond)
    assert independent.status == "clarification_required"
    assert independent.session_id != first.session_id
    assert len(requests) == 2  # Clarification does not call the knowledge backend.


def test_new_question_can_replace_pending_clarification():
    chats, requests = Conversations(), []
    def respond(question):
        requests.append(question)
        return no_evidence(question)
    first = chats.reply(ChatRequest(message="Can I pay monthly?"), respond)
    chats.reply(ChatRequest(message="Does home insurance cover storms?", session_id=first.session_id), respond)
    assert requests[-1].question == "Does home insurance cover storms?"
    assert requests[-1].product == "home"


def test_unclear_short_reply_does_not_destroy_pending_question():
    chats = Conversations()
    first = chats.reply(ChatRequest(message="Can I pay monthly?"), no_evidence)
    chats.reply(ChatRequest(message="not sure", session_id=first.session_id), no_evidence)
    assert chats.sessions[first.session_id].pending.question == "Can I pay monthly?"


def test_backend_failure_retains_pending_question_for_retry():
    chats = Conversations()
    first = chats.reply(ChatRequest(message="Can I pay monthly?"), no_evidence)
    def fail(_):
        raise RuntimeError("backend failed")
    with pytest.raises(RuntimeError):
        chats.reply(ChatRequest(message="car", session_id=first.session_id), fail)
    assert chats.sessions[first.session_id].pending.question == "Can I pay monthly?"
    retried = chats.reply(ChatRequest(message="car", session_id=first.session_id), no_evidence)
    assert retried.product == "car"


def test_greeting_and_product_selection_need_no_model():
    chats = Conversations()
    def unexpected(_):
        raise AssertionError("Should not call model")
    hello = chats.reply(ChatRequest(message="hi"), unexpected)
    selected = chats.reply(ChatRequest(message="home insurance", session_id=hello.session_id), unexpected)
    assert selected.product == "home"
    assert chats.sessions[hello.session_id].pending is None


def test_expiry_capacity_and_busy_session():
    clock = [0]
    chats = Conversations(ttl_seconds=10, max_sessions=1, clock=lambda: clock[0])
    first = chats.reply(ChatRequest(message="hi"), no_evidence)
    with pytest.raises(ChatBusy):
        chats.reply(ChatRequest(message="hi"), no_evidence)
    _, conversation = chats.acquire(first.session_id)
    try:
        with pytest.raises(ChatBusy):
            chats.reply(ChatRequest(message="hi", session_id=first.session_id), no_evidence)
        with pytest.raises(ChatBusy):
            chats.delete(first.session_id)
    finally:
        conversation.lock.release()
    clock[0] = 11
    with pytest.raises(ChatExpired):
        chats.reply(ChatRequest(message="home", session_id=first.session_id), no_evidence)
    assert chats.reply(ChatRequest(message="hi"), no_evidence).session_id != first.session_id


def test_empty_messages_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(message="   ")


def test_chat_api_multi_turn_sources_assets_auth_and_reset(monkeypatch, settings, store):
    import app.api as api
    from app.ingest import ingest_html
    from app.pipeline import build_graph
    for product in ("car", "home"):
        html = f'''<main><h2>Can I pay monthly or annually?</h2>
        <p>Synthetic {product} payment answer.</p><p>Synthetic {product} condition.</p></main>'''.encode()
        ingest_html(store, html, SitemapEntry(url=f"https://www.allianz.com.au/{product}-insurance.html"))
    settings.api_key = "test-key"
    monkeypatch.setattr(api, "Store", lambda _: store)
    monkeypatch.setattr(api, "verify_embedding_model", lambda _: None)
    monkeypatch.setattr(api, "build_graph", lambda store, config: build_graph(
        store, config, selector=lambda _: Selection(evidence_ids=[])))
    monkeypatch.setattr(store, "close", lambda: None)
    headers = {"X-API-Key": "test-key"}
    with TestClient(api.create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200 and 'id="chat-form"' in page.text
        assert "default-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/chat").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/assets/chat.js").status_code == 200
        assert client.get("/assets/chat.css").status_code == 200
        assert client.post("/chat", json={"message": "hi"}).status_code == 401
        first = client.post("/chat", headers=headers, json={"message": "Can I pay monthly or annually?"}).json()
        assert first["status"] == "clarification_required"
        session = first["session_id"]
        car = client.post("/chat", headers=headers, json={"message": "car", "session_id": session}).json()
        assert car["status"] == "answered" and car["product"] == "car"
        assert "Synthetic car condition." in car["answer"]
        assert car["sources"][0]["url"] == "https://www.allianz.com.au/car-insurance.html"
        client.post("/chat", headers=headers, json={"message": "home insurance", "session_id": session})
        home = client.post("/chat", headers=headers, json={"message": "Can I pay monthly or annually?", "session_id": session}).json()
        assert home["status"] == "answered" and home["product"] == "home"
        assert "Synthetic car" not in home["answer"]
        assert home["sources"][0]["url"] == "https://www.allianz.com.au/home-insurance.html"
        assert client.post("/chat", headers=headers, json={"message": "hi", "session_id": str(uuid4())}).status_code == 410
        assert client.delete(f"/chat/{session}", headers=headers).status_code == 200
        assert client.post("/chat", headers=headers, json={"message": "hi", "session_id": session}).status_code == 410
