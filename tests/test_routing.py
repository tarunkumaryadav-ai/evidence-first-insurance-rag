import pytest

from app.models import Question
from app.pipeline import build_graph
from app.routing import infer_products


@pytest.mark.parametrize("question, expected", [
    ("How much is Allianz car insurance?", {"car"}),
    ("Does home insurance cover storms?", {"home"}),
    ("What does business insurance cover?", {"business"}),
    ("Is my car covered for theft?", {"car"}),
    ("Does my car have cover while parked at home?", {"car"}),
    ("Does car insurance cover me when working from home?", {"car"}),
    ("Does home insurance cover my car?", {"home"}),
    ("Does business insurance cover my car?", {"business"}),
    ("Do I need commercial vehicle insurance?", {"business"}),
    ("Do I need commercial motor vehicle insurance?", {"business"}),
    ("Does car insurance cover a commercial vehicle?", {"car"}),
    ("What about workers' compensation?", {"business"}),
    ("What is public liability insurance?", {"business"}),
    ("Is there cover for my windscreen?", {"car"}),
    ("What about my contents?", {"home"}),
    ("Compare car and home insurance", {"car", "home"}),
    ("Compare car insurance with home insurance", {"car", "home"}),
    ("Car or business insurance?", {"car", "business"}),
    ("What is strata insurance?", {"home", "business"}),
    ("What is covered?", set()),
    ("Can I pay monthly or annually?", set()),
    ("Is the carpet covered?", set()),
    ("How does this apply to homework?", set()),
])
def test_product_inference(question, expected):
    assert infer_products(question) == expected


class RecordingStore:
    def __init__(self):
        self.requests = []

    def retrieve(self, question):
        self.requests.append(question)
        return []

    def close(self):
        pass


def test_inferred_product_reaches_retrieval(settings):
    store = RecordingStore()
    request = Question(question="Does home insurance cover storms?")
    result = build_graph(store, settings).invoke({"request": request})
    assert store.requests[0].product == "home"
    assert store.requests[0].question == request.question
    assert request.product is None  # Do not mutate caller input.
    assert result["response"].status == "abstained"


@pytest.mark.parametrize("question", ["What is covered?", "Compare car and home insurance"])
def test_unclear_product_clarifies_before_retrieval(settings, question):
    store = RecordingStore()
    result = build_graph(store, settings).invoke({"request": Question(question=question)})
    assert result["response"].status == "clarification_required"
    assert not store.requests


def test_explicit_product_still_supported(settings):
    store = RecordingStore()
    build_graph(store, settings).invoke({"request": Question(question="Can I pay monthly?", product="car")})
    assert store.requests[0].product == "car"


def test_conflicting_explicit_product_clarifies(settings):
    store = RecordingStore()
    result = build_graph(store, settings).invoke({
        "request": Question(question="Does home insurance cover storms?", product="car")})
    assert result["response"].status == "clarification_required"
    assert not store.requests


def test_question_only_api_routes_and_clarifies(monkeypatch, settings):
    from fastapi.testclient import TestClient
    import app.api as api
    store = RecordingStore()
    monkeypatch.setattr(api, "Store", lambda _: store)
    monkeypatch.setattr(api, "verify_embedding_model", lambda _: None)
    with TestClient(api.create_app(settings)) as client:
        response = client.post("/ask", json={"question": "Does home insurance cover storms?"})
        assert response.status_code == 200
        assert store.requests[0].product == "home"
        unclear = client.post("/ask", json={"question": "What is covered?"})
        assert unclear.json()["status"] == "clarification_required"
        assert len(store.requests) == 1
        schema = client.get("/openapi.json").json()["components"]["schemas"]["Question"]
        assert schema["required"] == ["question"]
        assert "product" not in schema["examples"][0]


def test_inferred_product_filters_real_retrieval_and_citations(store, settings):
    from app.ingest import ingest_html
    from app.models import SitemapEntry
    heading = "Does home insurance cover storms?"
    for product in ("car", "home"):
        html = f"<main><h2>{heading}</h2><p>Synthetic {product} passage only.</p></main>".encode()
        ingest_html(store, html, SitemapEntry(url=f"https://www.allianz.com.au/{product}-insurance.html"))
    def should_not_run(_):
        raise AssertionError("Exact FAQ should not invoke model selection")
    answer = build_graph(store, settings, selector=should_not_run).invoke(
        {"request": Question(question=heading)})["response"]
    assert answer.status == "answered"
    assert "Synthetic home passage only." in answer.answer
    assert "Synthetic car passage only." not in answer.answer
    assert answer.sources[0].url == "https://www.allianz.com.au/home-insurance.html"
