import hashlib
import json
import logging
from urllib.parse import urlsplit
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import httpx
import ollama
import portalocker

from app.config import Settings
from app.crawl import Crawler
from app.extract import EXTRACTOR_VERSION, extract_chunks
from app.models import SitemapEntry
from app.store import Store

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def verify_embedding_model(store: Store):
    """A tag can point to a new model later; reject silently mixed embeddings."""
    client = ollama.Client(host=store.settings.ollama_url, timeout=store.settings.timeout_seconds)
    models = client.list().models
    model = next((m for m in models if m.model == store.settings.embedding_model
                  or m.model == store.settings.embedding_model + ":latest"), None)
    if model is None:
        raise ValueError(f"Embedding model is not installed: {store.settings.embedding_model}")
    digest = model.digest
    with store.connect() as db:
        previous = db.execute("SELECT value FROM settings WHERE key='embedding_digest'").fetchone()
        if previous and previous[0] != digest:
            raise ValueError("Embedding model digest changed. Rebuild in a new data directory/collection.")
        db.execute("INSERT OR IGNORE INTO settings VALUES ('embedding_digest', ?)", (digest,))


def ingest_html(store: Store, html: bytes, entry: SitemapEntry, *, origin="import",
                global_products: list[str] | None = None, sub_product: str | None = None,
                variant: str | None = None, force: bool = False) -> str:
    checksum = hashlib.sha256(html).hexdigest()
    archive = store.settings.data_dir / "raw"
    archive.mkdir(parents=True, exist_ok=True)
    snapshot = archive / f"{checksum}.html"
    if not snapshot.exists():
        snapshot.write_bytes(html)
    signature = json.dumps([EXTRACTOR_VERSION, store.settings.chunk_chars,
                            global_products, sub_product, variant])
    timestamp = now()
    with store.connect() as db:
        page = db.execute("SELECT * FROM pages WHERE url=?", (entry.url,)).fetchone()
        first = db.execute("SELECT metadata FROM chunks WHERE url=? LIMIT 1", (entry.url,)).fetchone()
        meta = json.loads(first[0]) if first else {}
        if not force and page and page["content_hash"] == checksum and meta.get("ingestion_signature") == signature:
            db.execute("UPDATE pages SET lastmod=?, fetched_at=?, origin=? WHERE url=?",
                       (entry.lastmod, timestamp, origin, entry.url))
            for row in db.execute("SELECT id,metadata FROM chunks WHERE url=?", (entry.url,)).fetchall():
                metadata = json.loads(row["metadata"])
                metadata.update(sitemap_lastmod=entry.lastmod, fetched_at=timestamp, origin=origin)
                db.execute("UPDATE chunks SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"]))
            return "unchanged"
    chunks = extract_chunks(html, entry, store.settings.chunk_chars, fetched_at=timestamp,
                            global_products=global_products, sub_product=sub_product, variant=variant)
    for chunk in chunks:
        chunk.id = str(uuid5(NAMESPACE_URL, chunk.id + signature))
        chunk.metadata["ingestion_signature"] = signature
        chunk.metadata["origin"] = origin
    store.replace_page(entry.url, chunks, origin)
    return "indexed"


def sync(store: Store, settings: Settings, *, limit: int | None = None,
         product: str | None = None, url: str | None = None, force: bool = False) -> dict:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    with portalocker.Lock(str(settings.data_dir / "ingestion.lock"), timeout=1):
        with store.connect() as db:
            run = db.execute("INSERT INTO runs(started_at,status) VALUES (?, 'running')", (now(),)).lastrowid
        report = {"discovered": 0, "indexed": 0, "unchanged": 0, "removed": 0, "errors": []}
        try:
            verify_embedding_model(store)
            with Crawler(settings) as crawler:
                entries = crawler.discover()
                report["discovered"] = len(entries)
                from app.crawl import product_for_url
                selected = [e for e in entries if (not product or product_for_url(e.url) == product)
                            and (not url or e.url == url)]
                # Small validation runs cover the three landing pages first.
                selected.sort(key=lambda entry: (
                    urlsplit(entry.url).path.rstrip("/") not in
                    {f"/{name}-insurance{suffix}" for name in ("car", "home", "business")
                     for suffix in ("", ".html")}, entry.url))
                if not selected:
                    raise ValueError("No sitemap entries match the selected product")
                if limit:
                    selected = selected[:limit]
                for entry in selected:
                    try:
                        html, final_url = crawler.fetch(entry.url)
                        # A canonical redirect must stay in the selected product.
                        if product_for_url(final_url) != product_for_url(entry.url):
                            raise ValueError("Page redirected to a different product or system page")
                        outcome = ingest_html(store, html, entry, origin="sitemap", force=force)
                        report[outcome] += 1
                        logger.info("%s %s", outcome, entry.url)
                    except Exception as exc:
                        report["errors"].append({"url": entry.url, "error": str(exc)})
                        logger.exception("Failed ingestion: %s", entry.url)
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (404, 410):
                            store.remove_page(entry.url)
                            report["removed"] += 1
                # Prune only after a complete, unfiltered successful discovery+fetch run.
                if not limit and not product and not url and not report["errors"]:
                    active = {e.url for e in entries}
                    with store.connect() as db:
                        known = [r[0] for r in db.execute("SELECT url FROM pages WHERE origin='sitemap'")]
                    for url in set(known) - active:
                        store.remove_page(url)
                        report["removed"] += 1
            status = "partial" if report["errors"] else ("sample" if limit or product or url else "success")
        except Exception as exc:
            report["errors"].append({"error": str(exc)})
            status = "failed"
            logger.exception("Ingestion failed")
        with store.connect() as db:
            db.execute("UPDATE runs SET finished_at=?,status=?,details=? WHERE id=?",
                       (now(), status, json.dumps(report), run))
        return {"status": status, **report}
