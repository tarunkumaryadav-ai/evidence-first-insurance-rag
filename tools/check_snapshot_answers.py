"""Evaluate real archived HTML in an isolated store while ingestion continues."""
import hashlib
import argparse
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from app.config import Settings
from app.ingest import ingest_html, verify_embedding_model
from app.models import Question, SitemapEntry
from app.pipeline import build_graph
from app.store import Store

URL = "https://www.allianz.com.au/car-insurance.html"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--faq-only", action="store_true", help="Benchmark only the exact-FAQ shortcut")
    parser.add_argument("--output", type=Path, help="Save a model-specific report without overwriting earlier benchmarks")
    args = parser.parse_args()
    base = Settings()
    db = sqlite3.connect(base.data_dir / "manifest.sqlite3")
    try:
        row = db.execute("SELECT content_hash,lastmod FROM pages WHERE url=?", (URL,)).fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError("Ingest the car landing page before running this check")
    html = (base.data_dir / "raw" / f"{row[0]}.html").read_bytes()
    if hashlib.sha256(html).hexdigest() != row[0]:
        raise ValueError("Archived HTML does not match the recorded hash")
    report = {"model": base.llm_model, "source_url": URL,
              "exact_faq_lookup": base.exact_faq_lookup, "checks": [], "passed": False}
    with TemporaryDirectory(prefix="live-check-", dir=base.data_dir) as directory:
        settings = base.model_copy(update={"data_dir": Path(directory), "qdrant_url": None,
                                           "collection": "snapshot_check"})
        store = Store(settings)
        try:
            verify_embedding_model(store)
            ingest_html(store, html, SitemapEntry(url=URL, lastmod=row[1]), origin="snapshot-evaluation")
            graph = build_graph(store, settings)
            checks = [
                ("Can I pay monthly or annually?", "answered"),
                ("What is my exact personal premium in dollars for policy XYZ123?", "abstained"),
            ]
            for question, expected in checks[:1] if args.faq_only else checks:
                started = perf_counter()
                response = graph.invoke({"request": Question(question=question, product="car")})["response"]
                passed = response.status == expected
                if expected == "answered":
                    passed = passed and "monthly" in response.answer.lower() and bool(response.sources)
                check = {"question": question, "expected": expected, "passed": passed,
                         "seconds": round(perf_counter() - started, 2), "response": response.model_dump()}
                report["checks"].append(check)
                print(json.dumps(check, indent=2), flush=True)
            report["passed"] = all(check["passed"] for check in report["checks"])
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            store.close()
    path = args.output or base.data_dir / ("live_faq_check.json" if args.faq_only else "live_answer_check.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "model": base.llm_model, "report": str(path)}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
