import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.ingest import verify_embedding_model
from app.models import Answer, Question
from app.pipeline import build_graph
from app.store import Store
from app.chat import ChatBusy, ChatExpired, ChatRequest, ChatResponse, Conversations

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
