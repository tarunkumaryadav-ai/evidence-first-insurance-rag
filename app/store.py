"""Qdrant vectors plus a transactional SQLite source/active-revision manifest."""
import json
import re
import sqlite3
from threading import RLock
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import portalocker
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from app.config import Settings
from app.models import Chunk, Question
from app.extract import EXTRACTOR_VERSION, VARIANTS
from app.documents import PDF_EXTRACTOR_VERSION, pdf_term_score


def normalize_question(text: str | None) -> str:
    return " ".join(re.findall(r"\w+", (text or "").casefold()))


class StoreInUseError(RuntimeError):
    """The embedded index is owned by an existing application process."""


class Store:
    def __init__(self, settings: Settings, *, embedding=None, client=None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = settings.data_dir / "manifest.sqlite3"
        self.query_lock = RLock()
        self.lock = None
        # Embedded Qdrant cannot be opened by separate API/ingestion processes.
        # Fail clearly; use Qdrant server for concurrent production workers.
        if not settings.qdrant_url and client is None:
            self.lock = portalocker.Lock(str(settings.data_dir / "local-store.lock"), timeout=1)
            try:
                self.lock.acquire()
            except portalocker.exceptions.LockException:
                raise StoreInUseError(
                    "The local knowledge index is already in use by another process. "
                    "The chatbot may already be running at http://127.0.0.1:8000/. "
                    "Use python start.py, or stop the existing server before starting another server or ingestion."
                ) from None
        try:
            self.client = client or (QdrantClient(url=settings.qdrant_url,
                          api_key=settings.qdrant_api_key, timeout=settings.timeout_seconds)
                          if settings.qdrant_url else
                          QdrantClient(path=str(settings.data_dir / "qdrant"),
                                       force_disable_check_same_thread=True))
            self.embeddings = embedding or OllamaEmbeddings(model=settings.embedding_model,
                          base_url=settings.ollama_url,
                          client_kwargs={"timeout": settings.timeout_seconds})
            with self.connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS pages (
                        url TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                        lastmod TEXT, fetched_at TEXT NOT NULL, origin TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chunks (
                        id TEXT PRIMARY KEY, url TEXT NOT NULL, body TEXT NOT NULL,
                        metadata TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS chunks_url ON chunks(url);
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(id UNINDEXED, body);
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS runs (
                        id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
                        status TEXT, details TEXT
                    );
                    CREATE TABLE IF NOT EXISTS documents (
                        id TEXT PRIMARY KEY, filename TEXT NOT NULL, storage_name TEXT NOT NULL,
                        product TEXT NOT NULL, content_hash TEXT NOT NULL, uploaded_at TEXT NOT NULL
                    );
                """)
                existing = db.execute("SELECT value FROM settings WHERE key='embedding_model'").fetchone()
                if existing and existing[0] != settings.embedding_model:
                    raise ValueError("Embedding model changed: use a fresh data directory and collection")
                db.execute("INSERT OR IGNORE INTO settings VALUES ('embedding_model', ?)",
                           (settings.embedding_model,))
                collection = db.execute("SELECT value FROM settings WHERE key='collection'").fetchone()
                if collection and collection[0] != settings.collection:
                    raise ValueError("Collection changed: use a separate data directory")
                db.execute("INSERT OR IGNORE INTO settings VALUES ('collection', ?)", (settings.collection,))
        except Exception:
            if hasattr(self, "client"):
                self.client.close()
            if self.lock:
                self.lock.release()
            raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function("normalize_question", 1, normalize_question, deterministic=True)
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self.client.close()
        if self.lock:
            self.lock.release()

    def vector_store(self, *, create=False):
        if not self.client.collection_exists(self.settings.collection):
            if not create:
                return None
            dimension = len(self.embeddings.embed_query("embedding dimension probe"))
            self.client.create_collection(self.settings.collection,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))
            if self.settings.qdrant_url:
                for field in ("product", "scope", "applicable_products", "variant", "category"):
                    self.client.create_payload_index(self.settings.collection,
                        field_name=f"metadata.{field}", field_schema=models.PayloadSchemaType.KEYWORD,
                        wait=True)
        return QdrantVectorStore(client=self.client, collection_name=self.settings.collection,
                                 embedding=self.embeddings, validate_embeddings=False,
                                 validate_collection_config=False)

    def replace_page(self, url: str, chunks: list[Chunk], origin: str):
        vector = self.vector_store(create=True)
        # Stage new vectors before committing the active revision. Orphan vectors
        # after a crash are rejected by the SQLite membership check at retrieval.
        vector.add_documents([Document(page_content=c.text, metadata={**c.metadata, "chunk_id": c.id})
                              for c in chunks], ids=[c.id for c in chunks], batch_size=16)
        with self.connect() as db:
            previous = [r[0] for r in db.execute("SELECT id FROM chunks WHERE url=?", (url,))]
            db.execute("DELETE FROM chunks_fts WHERE id IN (SELECT id FROM chunks WHERE url=?)", (url,))
            db.execute("DELETE FROM chunks WHERE url=?", (url,))
            db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?)",
                [(c.id, url, c.text, json.dumps(c.metadata)) for c in chunks])
            db.executemany("INSERT INTO chunks_fts VALUES (?, ?)", [(c.id, c.text) for c in chunks])
            meta = chunks[0].metadata
            db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?)",
                (url, meta["content_hash"], meta["sitemap_lastmod"], meta["fetched_at"], origin))
        obsolete = sorted(set(previous) - {c.id for c in chunks})
        if obsolete:
            self.client.delete(self.settings.collection, models.PointIdsList(points=obsolete), wait=True)

    def remove_page(self, url: str):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE url=?", (url,))]
            db.execute("DELETE FROM chunks_fts WHERE id IN (SELECT id FROM chunks WHERE url=?)", (url,))
            db.execute("DELETE FROM chunks WHERE url=?", (url,))
            db.execute("DELETE FROM pages WHERE url=?", (url,))
        if ids and self.client.collection_exists(self.settings.collection):
            self.client.delete(self.settings.collection, models.PointIdsList(points=ids), wait=True)

    def retrieve(self, request: Question) -> list[dict]:
        # Sync API endpoints run on worker threads. Serialize local client access
        # after explicitly disabling its SQLite thread-affinity check.
        with self.query_lock:
            return self._retrieve(request)

    def _retrieve(self, request: Question) -> list[dict]:
        vector = self.vector_store()
        if vector is None:
            return []
        global_scope = models.FieldCondition(key="metadata.scope", match=models.MatchValue(value="global"))
        conditions = [models.Filter(should=[
            models.FieldCondition(key="metadata.product", match=models.MatchValue(value=request.product)),
            models.Filter(must=[global_scope,
                models.FieldCondition(key="metadata.applicable_products",
                                      match=models.MatchValue(value=request.product))])])]
        for key in ("variant", "category"):
            if value := getattr(request, key):
                match = models.FieldCondition(key=f"metadata.{key}", match=models.MatchValue(value=value))
                if key == "variant" and value in VARIANTS.get(request.product, {}):
                    # A product landing page can contain an authoritative overview for a
                    # named variant even though its URL correctly has no variant taxonomy.
                    conditions.append(models.Filter(should=[match,
                        models.IsNullCondition(is_null=models.PayloadField(key="metadata.variant"))]))
                else:
                    conditions.append(match)
        dense = vector.similarity_search_with_score(request.question,
                    k=self.settings.retrieve_k * 4, filter=models.Filter(must=conditions),
                    score_threshold=self.settings.min_similarity)
        ranks = {}
        for rank, (doc, _) in enumerate(dense):
            ranks[doc.metadata["chunk_id"]] = 1 / (60 + rank + 1)
        tokens = re.findall(r"[a-zA-Z0-9]{3,}", request.question.lower())
        stop = {"the", "and", "does", "what", "for", "with", "are", "can", "how", "insurance", "allianz"}
        expression = " OR ".join('"' + token + '"' for token in dict.fromkeys(tokens) if token not in stop)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.max_source_age_hours)
        with self.connect() as db:
            db.execute("BEGIN")  # Read the active revision and its section in one snapshot.
            # A FAQ answer may say only 'Yes' and omit all question keywords.
            # Look up its actual heading independently of vector/paragraph matching.
            exact = db.execute("""
                SELECT id FROM chunks WHERE
                  normalize_question(json_extract(metadata, '$.heading_path[#-1]'))=?
                  AND (json_extract(metadata, '$.product')=? OR
                    (json_extract(metadata, '$.scope')='global' AND EXISTS
                      (SELECT 1 FROM json_each(metadata, '$.applicable_products') WHERE value=?)))
            """, (normalize_question(request.question), request.product, request.product)).fetchall()
            for row in exact:
                ranks[row[0]] = 1 + ranks.get(row[0], 0)
            if expression:
                # Apply metadata filters before ranking lexical candidates.
                rows = db.execute("""
                    SELECT f.id FROM chunks_fts f JOIN chunks c ON c.id=f.id
                    WHERE chunks_fts MATCH ? AND (json_extract(c.metadata, '$.product')=?
                      OR (json_extract(c.metadata, '$.scope')='global' AND EXISTS (
                        SELECT 1 FROM json_each(c.metadata, '$.applicable_products') WHERE value=?)))
                    AND (? IS NULL OR json_extract(c.metadata, '$.variant')=?
                      OR (json_extract(c.metadata, '$.scope')='global'
                          AND json_extract(c.metadata, '$.variant') IS NULL))
                    AND (? IS NULL OR json_extract(c.metadata, '$.category')=?)
                    ORDER BY bm25(chunks_fts) LIMIT ?
                """, (expression, request.product, request.product, request.variant, request.variant,
                      request.category, request.category, self.settings.retrieve_k * 4)).fetchall()
                for rank, row in enumerate(rows):
                    ranks[row[0]] = ranks.get(row[0], 0) + 1 / (60 + rank + 1)
            # Promote strict, high-coverage matches from private PDFs so a similar
            # public article cannot crowd an exact contractual clause out of top-k.
            pdf_rows = db.execute("""
                SELECT id,body FROM chunks WHERE json_extract(metadata, '$.origin')='uploaded_pdf'
                AND (json_extract(metadata, '$.product')=? OR
                  (json_extract(metadata, '$.scope')='global' AND EXISTS (
                    SELECT 1 FROM json_each(metadata, '$.applicable_products') WHERE value=?)))
            """, (request.product, request.product)).fetchall()
            for row in pdf_rows:
                coverage, overlap = pdf_term_score(request.question, row["body"])
                if overlap >= 4 and coverage >= 0.75:
                    ranks[row["id"]] = max(ranks.get(row["id"], 0),
                                           2 + coverage + overlap / 100)
            candidates, seen_sections = [], set()
            for chunk_id in sorted(ranks, key=ranks.get, reverse=True):
                row = db.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
                if not row:
                    continue  # Never expose an uncommitted/obsolete vector revision.
                metadata = json.loads(row["metadata"])
                if metadata.get("extractor_version") not in {EXTRACTOR_VERSION, PDF_EXTRACTOR_VERSION}:
                    continue
                is_global = metadata.get("scope") == "global"
                if metadata["product"] != request.product and not (
                    is_global and request.product in metadata.get("applicable_products", [])):
                    continue
                if any(getattr(request, key) and metadata.get(key) != getattr(request, key)
                       and not (key == "variant" and metadata.get(key) is None
                                and getattr(request, key) in VARIANTS.get(request.product, {}))
                       for key in ("variant", "category")):
                    continue
                if metadata.get("origin") != "uploaded_pdf" and datetime.fromisoformat(metadata["fetched_at"]) < cutoff:
                    continue
                section = metadata["section_id"]
                if section in seen_sections:
                    continue
                # Reassemble all section chunks, preserving nearby qualifications.
                parts = db.execute("SELECT body FROM chunks WHERE url=? AND "
                    "json_extract(metadata, '$.section_id')=? ORDER BY rowid",
                    (row["url"], section)).fetchall()
                text = "\n\n".join(part[0] for part in parts)
                if len(text) > 16000:
                    continue  # Require a site-specific extraction rule rather than truncate conditions.
                seen_sections.add(section)
                candidates.append({"id": chunk_id, "text": text, "metadata": metadata})
                if len(candidates) >= self.settings.retrieve_k:
                    break
        return candidates

    def stats(self):
        with self.connect() as db:
            return {"pages": db.execute("SELECT count(*) FROM pages").fetchone()[0],
                    "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                    "latest_run": dict(row) if (row := db.execute(
                        "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()) else None}
