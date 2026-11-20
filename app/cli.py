import argparse
import json
import logging
from pathlib import Path

import ollama
import httpx
import portalocker

from app.config import Settings
from app.crawl import Crawler, parse_sitemap, product_for_url, safe_url
from app.ingest import ingest_html, sync, verify_embedding_model
from app.models import Question, SitemapEntry
from app.pipeline import build_graph
from app.store import Store, StoreInUseError


def main():
    parser = argparse.ArgumentParser(description="Allianz evidence-only RAG")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check installed Ollama models and environment")
    discovery = sub.add_parser("discover", help="Read sitemap, save product URLs and lastmod values")
    discovery.add_argument("--file", type=Path, help="Read a browser-saved XML URL sitemap offline")
    ingest = sub.add_parser("ingest", help="Incrementally synchronize sitemap pages")
    ingest.add_argument("--limit", type=int)
    ingest.add_argument("--product", choices=["car", "home", "business"])
    ingest.add_argument("--url", help="Refresh only this URL, which must be present in the sitemap")
    ingest.add_argument("--force", action="store_true", help="Re-extract/re-embed even unchanged HTML")
    local = sub.add_parser("import-html", help="Index an authorized local HTML export")
    local.add_argument("file", type=Path)
    local.add_argument("--url", required=True)
    local.add_argument("--lastmod")
    local.add_argument("--sub-product")
    local.add_argument("--variant")
    local.add_argument("--global-for", nargs="+", choices=["car", "home", "business"])
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--product", choices=["car", "home", "business"])
    ask.add_argument("--variant")
    ask.add_argument("--category")
    sub.add_parser("stats")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if args.command == "doctor":
        models = ollama.Client(host=settings.ollama_url, timeout=settings.timeout_seconds).list().models
        installed = {m.model for m in models}
        required = {settings.llm_model, settings.embedding_model}
        missing = [name for name in required if name not in installed and name + ":latest" not in installed]
        print(json.dumps({"ollama_models": sorted(installed), "missing": missing,
                          "qdrant_mode": "server" if settings.qdrant_url else "embedded",
                          "data_dir": str(settings.data_dir)}, indent=2))
        return 1 if missing else 0
    if args.command == "discover":
        if args.file:
            children, entries = parse_sitemap(args.file.read_bytes())
            if children:
                raise ValueError("Saved file is a sitemap index; save its child URL sitemaps instead")
            entries = [entry for entry in entries if product_for_url(entry.url)]
            if not entries:
                raise ValueError("No supported product URLs in the saved sitemap")
        else:
            with Crawler(settings) as crawler:
                entries = crawler.discover()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / "sitemap.json"
        path.write_text(json.dumps([entry.model_dump() for entry in entries], indent=2), encoding="utf-8")
        print(json.dumps({"urls": len(entries), "saved": str(path)}, indent=2))
        return 0
    store = Store(settings)
    try:
        if args.command == "ingest":
            result = sync(store, settings, limit=args.limit, product=args.product,
                          url=args.url, force=args.force)
            print(json.dumps(result, indent=2))
            return 1 if result["errors"] else 0
        if args.command == "import-html":
            verify_embedding_model(store)
            entry = SitemapEntry(url=safe_url(args.url), lastmod=args.lastmod)
            result = ingest_html(store, args.file.read_bytes(), entry, global_products=args.global_for,
                                 sub_product=args.sub_product, variant=args.variant)
            print(json.dumps({"status": result, "url": entry.url}))
        elif args.command == "ask":
            verify_embedding_model(store)
            request = Question(question=args.question, product=args.product,
                               variant=args.variant, category=args.category)
            answer = build_graph(store, settings).invoke({"request": request})["response"]
            print(answer.model_dump_json(indent=2))
        else:
            print(json.dumps(store.stats(), indent=2))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, ollama.ResponseError, ValueError, OSError, StoreInUseError,
            portalocker.exceptions.LockException) as exc:
        logging.getLogger(__name__).error("%s: %s", type(exc).__name__, exc)
        raise SystemExit(1) from None
