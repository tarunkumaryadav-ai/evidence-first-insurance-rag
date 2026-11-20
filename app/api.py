import hmac
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.ingest import verify_embedding_model
from app.models import Answer, Question
from app.pipeline import build_graph
from app.store import Store
from app.chat import ChatBusy, ChatExpired, ChatRequest, ChatResponse, Conversations
from app.documents import (evidence_html, extract_pdf_chunks, safe_filename,
                           signature as document_signature, verify_signature)
from app.ingest import now

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings)
        try:
            verify_embedding_model(store)
            app.state.store = store
            app.state.graph = build_graph(store, settings)
            app.state.conversations = Conversations()
            yield
        finally:
            store.close()

    app = FastAPI(title="Allianz evidence-only RAG", version="0.1.0", lifespan=lifespan)
    web = Path(__file__).resolve().parent / "web"
    app.mount("/assets", StaticFiles(directory=web), name="chat_assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web / "index.html", headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/chat", include_in_schema=False)
    def chat_page():
        return index()

    def authorize(x_api_key: str | None = Header(default=None)):
        if settings.api_key and not hmac.compare_digest(x_api_key or "", settings.api_key):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", dependencies=[Depends(authorize)])
    def ready(request: Request):
        try:
            verify_embedding_model(request.app.state.store)
            store = request.app.state.store
            stats = store.stats()
            if not store.client.collection_exists(settings.collection) or not stats["chunks"]:
                raise ValueError("No indexed content")
            return {"status": "ready", **stats}
        except Exception:
            logger.exception("Readiness check failed")
            raise HTTPException(status_code=503, detail="Knowledge service is not ready") from None

    @app.get("/stats", dependencies=[Depends(authorize)])
    def stats(request: Request):
        return {**request.app.state.store.stats(),
                "models": {"llm": settings.llm_model, "embedding": settings.embedding_model}}

    @app.post("/documents", dependencies=[Depends(authorize)])
    async def upload_document(request: Request, product: str = Query(pattern="^(car|home|business)$"),
                              filename: str = Query(min_length=1, max_length=200)):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/pdf":
            raise HTTPException(status_code=415, detail="Upload must use Content-Type: application/pdf")
        declared = request.headers.get("content-length")
        if declared and int(declared) > settings.upload_max_bytes:
            raise HTTPException(status_code=413, detail="PDF exceeds the configured upload limit")
        content = bytearray()
        async for part in request.stream():
            content.extend(part)
            if len(content) > settings.upload_max_bytes:
                raise HTTPException(status_code=413, detail="PDF exceeds the configured upload limit")
        data = bytes(content)
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise HTTPException(status_code=400, detail="File does not have a valid PDF signature")
        document_id = uuid4().hex
        cleaned = safe_filename(filename)
        uploaded_at = now()
        try:
            chunks = extract_pdf_chunks(data, document_id=document_id, filename=cleaned,
                                        product=product, max_chars=settings.chunk_chars,
                                        fetched_at=uploaded_at)
            directory = settings.data_dir / "documents"
            directory.mkdir(parents=True, exist_ok=True)
            storage_name = document_id + ".pdf"
            target = directory / storage_name
            target.write_bytes(data)
            checksum = hashlib.sha256(data).hexdigest()
            with request.app.state.store.connect() as db:
                db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                           (document_id, cleaned, storage_name, product, checksum, uploaded_at))
            request.app.state.store.replace_page(f"document://{document_id}", chunks, "uploaded_pdf")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except Exception:
            logger.exception("PDF ingestion failed")
            if 'target' in locals():
                target.unlink(missing_ok=True)
            with request.app.state.store.connect() as db:
                db.execute("DELETE FROM documents WHERE id=?", (document_id,))
            raise HTTPException(status_code=503, detail="PDF could not be indexed") from None
        return {"id": document_id, "filename": cleaned, "product": product,
                "pages_with_text": len({c.metadata['page_number'] for c in chunks}),
                "chunks": len(chunks)}

    @app.get("/documents", dependencies=[Depends(authorize)])
    def list_documents(request: Request):
        with request.app.state.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,filename,product,uploaded_at FROM documents ORDER BY uploaded_at DESC")]

    @app.delete("/documents/{document_id}", dependencies=[Depends(authorize)])
    def delete_document(document_id: str, request: Request):
        with request.app.state.store.connect() as db:
            row = db.execute("SELECT storage_name FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        request.app.state.store.remove_page(f"document://{document_id}")
        with request.app.state.store.connect() as db:
            db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        (settings.data_dir / "documents" / row["storage_name"]).unlink(missing_ok=True)
        return {"status": "deleted"}

    def document_evidence(request: Request, document_id: str, chunk: str):
        with request.app.state.store.connect() as db:
            document = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            evidence = db.execute("SELECT body,metadata FROM chunks WHERE id=?", (chunk,)).fetchone()
        if not document or not evidence:
            raise HTTPException(status_code=404, detail="Document evidence not found")
        metadata = json.loads(evidence["metadata"])
        if metadata.get("document_id") != document_id:
            raise HTTPException(status_code=404, detail="Document evidence not found")
        return document, evidence, metadata

    @app.get("/documents/{document_id}/evidence", response_class=HTMLResponse, include_in_schema=False)
    def view_evidence(document_id: str, request: Request, chunk: str, page: int,
                      expires: int, signature: str):
        try:
            verify_signature(settings, document_id, chunk, page, expires, signature)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        document, evidence, metadata = document_evidence(request, document_id, chunk)
        if int(metadata.get("page_number", 0)) != page:
            raise HTTPException(status_code=403, detail="Source page mismatch")
        query = f"chunk={chunk}&page={page}&expires={expires}&signature={signature}"
        response = HTMLResponse(evidence_html(document["filename"], page, evidence["body"],
                                              f"/documents/{document_id}/file?{query}"))
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
        return response

    @app.get("/documents/{document_id}/file", include_in_schema=False)
    def view_document(document_id: str, request: Request, chunk: str, page: int,
                      expires: int, signature: str):
        try:
            verify_signature(settings, document_id, chunk, page, expires, signature)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        document, _, metadata = document_evidence(request, document_id, chunk)
        if int(metadata.get("page_number", 0)) != page:
            raise HTTPException(status_code=403, detail="Source page mismatch")
        response = FileResponse(settings.data_dir / "documents" / document["storage_name"],
                                media_type="application/pdf", filename=document["filename"],
                                content_disposition_type="inline")
        response.headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        return response

    @app.post("/ask", response_model=Answer, dependencies=[Depends(authorize)])
    def ask(question: Question, request: Request):
        try:
            verify_embedding_model(request.app.state.store)
            return request.app.state.graph.invoke({"request": question})["response"]
        except Exception:
            logger.exception("Question processing failed")
            raise HTTPException(status_code=503, detail="Knowledge service is temporarily unavailable") from None

    @app.post("/chat", response_model=ChatResponse, dependencies=[Depends(authorize)])
    def chat_turn(message: ChatRequest, request: Request):
        def answer_question(question):
            verify_embedding_model(request.app.state.store)
            return request.app.state.graph.invoke({"request": question})["response"]
        try:
            return request.app.state.conversations.reply(message, answer_question)
        except ChatExpired as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from None
        except ChatBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception:
            logger.exception("Chat processing failed")
            raise HTTPException(status_code=503, detail="Could not complete this turn. Your conversation is retained; try again.") from None

    @app.delete("/chat/{session_id}", dependencies=[Depends(authorize)])
    def delete_chat(session_id: UUID, request: Request):
        try:
            request.app.state.conversations.delete(session_id)
        except ChatBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"status": "cleared"}

    return app


app = create_app()
