"""Explicit live Ollama check; synthetic content lives only in a temporary store."""
import json
from tempfile import TemporaryDirectory

from app.config import Settings
from app.ingest import ingest_html, verify_embedding_model
from app.models import Question, SitemapEntry
from app.pipeline import build_graph
from app.store import Store

HTML = b"""<html><head><title>SYNTHETIC INTEGRATION TEST ONLY</title></head>
<body><main><h1>Example car insurance</h1><h2>Example cover</h2>
<!-- Synthetic fixture only. Never insert in the real Allianz index. -->
<p>The example policy covers accidental damage. Wear and tear is excluded.</p>
</main></body></html>"""


def main():
    base = Settings()
    base.data_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="smoke-", dir=base.data_dir) as directory:
        settings = Settings(data_dir=directory, qdrant_url=None, collection="smoke_test",
                            timeout_seconds=180)
        store = Store(settings)
        try:
            verify_embedding_model(store)
            ingest_html(store, HTML, SitemapEntry(
                url="https://www.allianz.com.au/car-insurance/synthetic-test.html"), origin="synthetic-test")
            graph = build_graph(store, settings)
            positive_state = graph.invoke({"request": Question(
                question="Does the example policy cover accidental damage?", product="car")})
            positive = positive_state["response"]
            print(json.dumps({"check": "answerable", "evidence_count": len(positive_state.get("evidence", [])),
                              "model_selection": positive_state.get("selection_response"),
                              "selection": positive_state.get("selection", {}).model_dump()
                              if positive_state.get("selection") else {},
                              "response": positive.model_dump()}, indent=2), flush=True)
            negative = graph.invoke({"request": Question(question="What is the policy premium in dollars?", product="car")})["response"]
            print(json.dumps({"check": "unsupported", "response": negative.model_dump()}, indent=2), flush=True)
            assert positive.status == "answered" and "accidental damage" in positive.answer
            assert negative.status == "abstained"
            print(json.dumps({"result": "passed", "embedding": settings.embedding_model,
                              "llm": settings.llm_model, "positive_status": positive.status,
                              "unsupported_status": negative.status,
                              "data": "synthetic; temporary database removed after check"}, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
