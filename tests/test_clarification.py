import pytest

from app.chat import ChatRequest, Conversations
from app.models import Answer, Question
from app.pipeline import build_graph
from app.routing import clarify_scope


@pytest.mark.parametrize("question, product, kind", [
    ("my car broke", "car", "intent"),
    ("My car broke down", "car", "intent"),
    ("My vehicle won't start", "car", "intent"),
    ("My car is damaged", "car", "intent"),
    ("My car broke. Are repairs covered?", "car", "incident"),
    ("What does car insurance cover?", "car", "variant"),
    ("What does home insurance cover?", "home", "variant"),
    ("What does business insurance cover?", "business", "topic"),
])
def test_vague_queries_get_scoping_question(question, product, kind):
    answer = clarify_scope(Question(question=question, product=product))
    assert answer.status == "clarification_required"
    assert answer.clarification_kind == kind
    assert answer.choices and not answer.sources


@pytest.mark.parametrize("question", [
    "Can I pay monthly or annually?", "How much is Allianz car insurance?",
    "Does car insurance cover theft?", "How do I make a claim?",
    "Compare the car insurance options", "My car broke down. I need roadside assistance.",
    "My engine stopped working. Does insurance cover repairs?",
])
def test_clear_questions_do_not_get_unnecessary_scope_question(question):
    assert clarify_scope(Question(question=question, product="car")) is None


def test_graph_clarifies_before_retrieval_or_model(settings):
    class NoLookup:
        def retrieve(self, _):
            raise AssertionError("No lookup should run for an unclear problem")
    def no_model(_):
        raise AssertionError("No model should run for clarification")
    result = build_graph(NoLookup(), settings, selector=no_model).invoke(
        {"request": Question(question="my car broke")})["response"]
    assert result.clarification_kind == "intent"


def recorder():
    calls = []
    def search(question):
        calls.append(question)
        return Answer(status="abstained", answer="No evidence in this test fixture.")
    return calls, search


def test_repair_clarification_chain_preserves_original_and_searches_once():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="my car broke"), search)
    assert first.clarification_kind == "intent" and not calls
    second = chats.reply(ChatRequest(message="Repair coverage", session_id=first.session_id), search)
    assert second.clarification_kind == "incident" and not calls
    third = chats.reply(ChatRequest(message="Mechanical breakdown", session_id=first.session_id), search)
    assert third.status == "abstained" and len(calls) == 1
    assert "my car broke" in calls[0].question
    assert "repair costs" in calls[0].question
    assert "Mechanical breakdown" in calls[0].question
    assert calls[0].product == "car"


def test_roadside_choice_goes_to_search_without_extra_cause_question():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="my car broke"), search)
    chats.reply(ChatRequest(message="Roadside assistance", session_id=first.session_id), search)
    assert len(calls) == 1
    assert "my car broke" in calls[0].question and "roadside assistance" in calls[0].question


def test_uncertain_reply_retains_pending_intent_without_a_model():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="my car broke"), search)
    repeated = chats.reply(ChatRequest(message="not sure", session_id=first.session_id), search)
    assert repeated.clarification_kind == "intent" and not calls
    assert chats.sessions[first.session_id].pending.question == "my car broke"


def test_other_incident_asks_for_description_then_preserves_it():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="my car broke. Are repairs covered?"), search)
    other = chats.reply(ChatRequest(message="Something else", session_id=first.session_id), search)
    assert "describe" in other.answer.lower() and not calls
    chats.reply(ChatRequest(message="Fluid is leaking under the car", session_id=first.session_id), search)
    assert len(calls) == 1 and "Fluid is leaking under the car" in calls[0].question


def test_plan_reply_applies_variant_and_remembers_it_for_followup():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="What does car insurance cover?"), search)
    assert first.clarification_kind == "variant" and not calls
    chats.reply(ChatRequest(message="Comprehensive Essentials", session_id=first.session_id), search)
    assert calls[-1].variant == "comprehensive-essentials"
    chats.reply(ChatRequest(message="What about theft?", session_id=first.session_id), search)
    assert calls[-1].variant == "comprehensive-essentials"


def test_unsure_plan_searches_comparison_without_assuming_variant():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="What does car insurance cover?"), search)
    chats.reply(ChatRequest(message="Compare the options", session_id=first.session_id), search)
    assert len(calls) == 1 and calls[0].variant is None
    assert "compare" in calls[0].question.lower()


def test_business_topic_narrows_search_and_topic_switch_drops_car_problem():
    chats = Conversations()
    calls, search = recorder()
    first = chats.reply(ChatRequest(message="What does business insurance cover?"), search)
    assert first.clarification_kind == "topic"
    chats.reply(ChatRequest(message="Public liability", session_id=first.session_id), search)
    assert calls[-1].product == "business" and "Public liability" in calls[-1].question
    car = chats.reply(ChatRequest(message="my car broke"), search)
    chats.reply(ChatRequest(message="Does home insurance cover storms?", session_id=car.session_id), search)
    assert calls[-1].product == "home" and "broke" not in calls[-1].question


def test_chat_api_clarifies_without_ollama_or_knowledge_lookup(monkeypatch, settings):
    import app.api as api
    from fastapi.testclient import TestClient
    checks = []
    class NoLookup:
        def retrieve(self, _):
            raise AssertionError("Ambiguous turns must not retrieve")
        def close(self):
            pass
    monkeypatch.setattr(api, "Store", lambda _: NoLookup())
    monkeypatch.setattr(api, "verify_embedding_model", lambda _: checks.append("startup"))
    with TestClient(api.create_app(settings)) as client:
        first = client.post("/chat", json={"message": "my car broke"}).json()
        assert first["clarification_kind"] == "intent"
        assert "Roadside assistance" in first["choices"]
        second = client.post("/chat", json={"message": "Repair coverage", "session_id": first["session_id"]}).json()
        assert second["clarification_kind"] == "incident"
        assert "Mechanical breakdown" in second["choices"]
        assert checks == ["startup"]  # Neither message contacted Ollama.
