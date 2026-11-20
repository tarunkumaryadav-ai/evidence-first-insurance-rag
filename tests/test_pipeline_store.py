from datetime import datetime, timedelta, timezone

import pytest

from app.extract import extract_chunks
from app.ingest import ingest_html
from app.models import Question, Selection, SitemapEntry
from app.pipeline import build_graph, render_answer

URL = "https://www.allianz.com.au/car-insurance/comprehensive.html"


def test_index_replace_and_idempotency(store, html):
    entry = SitemapEntry(url=URL)
    assert ingest_html(store, html, entry) == "indexed"
    count = store.stats()["chunks"]
    assert ingest_html(store, html, entry) == "unchanged"
    assert store.stats()["chunks"] == count
    changed = html.replace(b"accidental damage", b"theft")
    assert ingest_html(store, changed, entry) == "indexed"
    assert store.stats()["chunks"] == count
    rows = store.retrieve(Question(question="theft cover", product="car"))
    assert rows and any("theft" in row["text"] for row in rows)
    assert all("accidental damage" not in row["text"] for row in rows)


def test_force_reprocesses_unchanged_html_without_duplicate_chunks(store, html):
    entry = SitemapEntry(url=URL)
    ingest_html(store, html, entry)
    count = store.stats()["chunks"]
    assert ingest_html(store, html, entry, force=True) == "indexed"
    assert store.stats()["chunks"] == count


def test_product_filter_and_unknown_variant(store, html):
    ingest_html(store, html, SitemapEntry(url=URL))
    assert not store.retrieve(Question(question="accidental damage", product="home"))
    assert not store.retrieve(Question(question="accidental damage", product="car", variant="unknown"))


def test_known_variant_can_use_product_landing_page_overview(store, html):
    landing = "https://www.allianz.com.au/car-insurance.html"
    ingest_html(store, html, SitemapEntry(url=landing))
    rows = store.retrieve(Question(question="comprehensive accidental damage", product="car",
                                   variant="comprehensive"))
    assert rows and rows[0]["metadata"]["variant"] is None


def test_stale_sources_not_returned(store, html):
    timestamp = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    chunks = extract_chunks(html, SitemapEntry(url=URL), fetched_at=timestamp)
    store.replace_page(URL, chunks, "test")
    assert not store.retrieve(Question(question="accidental damage", product="car"))


def test_obsolete_extractor_revision_not_returned(store, html):
    import json
    ingest_html(store, html, SitemapEntry(url=URL))
    with store.connect() as db:
        for row in db.execute("SELECT id, metadata FROM chunks").fetchall():
            metadata = json.loads(row["metadata"])
            metadata["extractor_version"] = "obsolete"
            db.execute("UPDATE chunks SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"]))
    assert not store.retrieve(Question(question="accidental damage", product="car"))


def test_uncommitted_vectors_cannot_become_evidence(store, html):
    chunks = extract_chunks(html, SitemapEntry(url=URL))
    store.replace_page(URL, chunks, "test")
    with store.connect() as db:
        db.execute("DELETE FROM chunks_fts")
        db.execute("DELETE FROM chunks")
    assert not store.retrieve(Question(question="accidental damage", product="car"))


def test_remove_page(store, html):
    ingest_html(store, html, SitemapEntry(url=URL))
    store.remove_page(URL)
    assert store.stats()["chunks"] == 0
    assert not store.retrieve(Question(question="accidental damage", product="car"))


def test_graph_requires_product_before_retrieval(store, settings):
    graph = build_graph(store, settings, selector=lambda _: Selection(evidence_ids=[]))
    result = graph.invoke({"request": Question(question="What is covered?")})["response"]
    assert result.status == "clarification_required"
    assert result.discovery_url


def test_empty_index_abstains_without_model_call(store, settings):
    def should_not_run(_):
        raise AssertionError("No model invocation expected")
    result = build_graph(store, settings, selector=should_not_run).invoke(
        {"request": Question(question="Is accidental damage covered?", product="car")})["response"]
    assert result.status == "abstained" and not result.sources


def test_graph_returns_only_verbatim_evidence(store, settings, html):
    ingest_html(store, html, SitemapEntry(url=URL))
    selected = []
    def choose(state):
        item = next(e for e in state["evidence"] if "accidental damage" in e["text"])
        selected.append(item)
        return Selection(evidence_ids=[item["id"]])
    result = build_graph(store, settings, selector=choose).invoke(
        {"request": Question(question="accidental damage", product="car")})["response"]
    assert result.status == "answered"
    assert selected[0]["text"] in result.answer
    assert "wear and tear" in result.answer
    assert result.sources[0].url == URL


def test_literal_uploaded_pdf_phrase_bypasses_model_selector(store, settings):
    from app.models import Chunk
    timestamp = datetime.now(timezone.utc).isoformat()
    text = ("A shared access contact is authorised to manage your policy and holds the same "
            "authority to make changes, cancel and claim until you remove them.\n"
            "There is no cover if your car is used for driver education at more than 100km/hour.")
    metadata = {"source_url": "document://doc1", "document_id": "doc1",
                "document_filename": "policy.pdf", "page_number": 35, "product": "car",
                "scope": "product", "applicable_products": ["car"], "variant": None,
                "category": "general", "heading_path": ["policy.pdf", "Page 35"],
                "sitemap_lastmod": None, "fetched_at": timestamp, "content_hash": "test",
                "section_id": "pdf-page-35", "extractor_version": "pdf-v1",
                "origin": "uploaded_pdf"}
    store.replace_page("document://doc1", [Chunk(id="11111111-1111-4111-8111-111111111111", text=text, metadata=metadata)],
                       "uploaded_pdf")
    def should_not_run(_):
        raise AssertionError("Literal PDF phrase should bypass the model selector")
    result = build_graph(store, settings, selector=should_not_run).invoke({"request": Question(
        question="What is a shared access contact and what authority do they have?", product="car")})["response"]
    assert result.status == "answered" and "shared access contact" in result.answer
    assert result.sources[0].page_number == 35
    speed = build_graph(store, settings, selector=should_not_run).invoke({"request": Question(
        question="Does the policy cover using my car for driver education above 100 km/h?",
        product="car")})["response"]
    assert speed.status == "answered" and "100km/hour" in speed.answer
    assert "No. The policy states:" in speed.answer
    assert speed.sources[0].excerpt == (
        "There is no cover if your car is used for driver education at more than 100km/hour.")


def test_invented_citation_rejected(store, html):
    ingest_html(store, html, SitemapEntry(url=URL))
    evidence = store.retrieve(Question(question="accidental damage", product="car"))
    result = render_answer(evidence, Selection(evidence_ids=["made-up-id"]))
    assert result.status == "abstained" and not result.sources
    assert result.sources_checked == [URL]


def test_model_can_abstain_despite_retrieval(store, settings, html):
    ingest_html(store, html, SitemapEntry(url=URL))
    graph = build_graph(store, settings, selector=lambda _: Selection(evidence_ids=[]))
    result = graph.invoke({"request": Question(question="unrelated question", product="car")})["response"]
    assert result.status == "abstained"


def test_global_content_only_matches_explicit_applicable_products(store, html):
    entry = SitemapEntry(url="https://www.allianz.com.au/shared-information.html")
    ingest_html(store, html, entry, global_products=["car", "home"])
    assert store.retrieve(Question(question="accidental damage", product="car", variant="comprehensive"))
    assert store.retrieve(Question(question="accidental damage", product="home"))
    assert not store.retrieve(Question(question="accidental damage", product="business"))


def test_taxonomy_changes_replace_old_revision(store, html):
    entry = SitemapEntry(url=URL)
    ingest_html(store, html, entry, sub_product="example-a")
    first_ids = {row[0] for row in _rows(store, "SELECT id FROM chunks")}
    assert ingest_html(store, html, entry, sub_product="example-b") == "indexed"
    second_ids = {row[0] for row in _rows(store, "SELECT id FROM chunks")}
    assert first_ids.isdisjoint(second_ids)
    rows = store.retrieve(Question(question="accidental damage", product="car"))
    assert rows and all(row["metadata"]["sub_product"] == "example-b" for row in rows)


def _rows(store, sql):
    with store.connect() as db:
        return db.execute(sql).fetchall()


def test_embedded_database_can_be_queried_from_api_worker_thread(settings, html):
    from concurrent.futures import ThreadPoolExecutor
    from conftest import FakeEmbeddings
    from app.store import Store
    value = Store(settings, embedding=FakeEmbeddings())
    try:
        ingest_html(value, html, SitemapEntry(url=URL))
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(value.retrieve,
                Question(question="accidental damage", product="car")).result()
        assert result
    finally:
        value.close()


def test_model_schema_requires_explicit_evidence_selection():
    from pydantic import ValidationError
    import pytest
    assert "evidence_ids" in Selection.model_json_schema()["required"]
    with pytest.raises(ValidationError):
        Selection.model_validate_json("{}")


@pytest.mark.parametrize("output, expected", [
    ('{"evidence_id": "E1"}', "answered"),
    ('{"evidence_id": "invented"}', "abstained"),
    ('{"evidence_id": "NONE"}', "abstained"),
    ('not valid JSON', "abstained"),
])
def test_model_output_validation_path(monkeypatch, store, settings, html, output, expected):
    from types import SimpleNamespace
    import json
    import app.pipeline as pipeline
    ingest_html(store, html, SitemapEntry(url=URL))
    class Model:
        def invoke(self, messages):
            payload = json.loads(messages[1].content)
            assert payload["evidence"][0]["evidence_id"] == "E1"
            assert "source_url" not in payload["evidence"][0]
            return SimpleNamespace(content=output)
    monkeypatch.setattr(pipeline, "ChatOllama", lambda **_: Model())
    response = build_graph(store, settings).invoke(
        {"request": Question(question="accidental damage", product="car")})["response"]
    assert response.status == expected


def test_whitespace_question_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Question(question="     ")


def test_exact_faq_returns_complete_quote_without_model(store, settings):
    html = b'''<main><h2>Can I pay monthly or annually?</h2>
    <p>Synthetic payment answer.</p><p>Synthetic payment condition.</p></main>'''
    ingest_html(store, html, SitemapEntry(url=URL))
    def should_not_run(_):
        raise AssertionError("An exact FAQ match should not invoke the model")
    result = build_graph(store, settings, selector=should_not_run).invoke(
        {"request": Question(question="Can I pay monthly or annually?", product="car")})["response"]
    assert result.status == "answered"
    assert "Synthetic payment condition." in result.answer
    assert result.sources[0].url == URL


def test_disagreeing_exact_faq_passages_do_not_bypass_model(store, settings):
    for name, answer in [("a", "Synthetic answer A."), ("b", "Synthetic answer B.")]:
        html = f'<main><h2>Can I pay monthly or annually?</h2><p>{answer}</p></main>'.encode()
        ingest_html(store, html, SitemapEntry(url=f"https://www.allianz.com.au/car-insurance/{name}.html"))
    calls = []
    def reject(state):
        calls.append(state)
        return Selection(evidence_ids=[])
    result = build_graph(store, settings, selector=reject).invoke(
        {"request": Question(question="Can I pay monthly or annually?", product="car")})["response"]
    assert calls and result.status == "abstained"
