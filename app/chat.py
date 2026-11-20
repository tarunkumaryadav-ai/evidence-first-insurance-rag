"""Bounded, tab-scoped conversations. Conversation text is never evidence."""
import re
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import Answer, Product, Question
from app.routing import clarify_scope, infer_products, infer_variant
from app.store import normalize_question


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value):
        return value.strip() if isinstance(value, str) else value


class ChatResponse(Answer):
    session_id: UUID
    product: Product | None = None


class ChatExpired(Exception):
    pass


class ChatBusy(Exception):
    pass


@dataclass
class Conversation:
    touched: float
    product: Product | None = None
    pending: Question | None = None
    pending_kind: str | None = None
    variant: str | None = None
    last_question: str | None = None
    lock: Lock = field(default_factory=Lock)


def product_reply(message: str, products: set[str]) -> bool:
    words = normalize_question(message).split()
    fillers = {"car", "cars", "motor", "home", "house", "business", "insurance",
               "please", "the", "one", "i", "mean", "about", "for", "yes", "it", "s"}
    return len(products) == 1 and len(words) <= 8 and set(words) <= fillers


def clarified_request(original: Question, kind: str, message: str) -> Question | None:
    text = normalize_question(message)
    variant = original.variant
    detail = None
    if kind == "intent":
        if re.search(r"\b(?:roadside|assistance|tow|towing)\b", text):
            detail = "I want roadside assistance information for this problem."
        elif re.search(r"\b(?:claims?|claiming)\b", text):
            detail = "I want to know how to make a claim for this problem."
        elif re.search(r"\b(?:cover|covered|coverage|repair|repairs|costs?)\b", text):
            detail = "I want to know whether insurance covers repair costs for this problem."
    elif kind == "incident":
        if text not in {"not sure", "unsure", "i don t know", "something else", "other", "yes", "no"}:
            detail = "User-described incident: " + message
    elif kind == "variant":
        variant = infer_variant(message, original.product)
        if variant:
            detail = "The insurance cover I mean is " + message
        elif re.search(r"\b(?:compare|comparison|unsure)\b|not sure|don t know", text):
            variant = None
            detail = f"Please compare the {original.product} insurance options."
    elif kind == "topic":
        if text not in {"not sure", "unsure", "yes", "no"} and len(text) >= 3:
            detail = "The business insurance topic I mean is " + message
    if detail is None:
        return None
    query = original.question + "\n" + detail
    if len(query) > 2000:
        return None  # Ask for a self-contained question rather than truncate constraints.
    return original.model_copy(update={"question": query, "variant": variant})


class Conversations:
    def __init__(self, *, ttl_seconds=3600, max_sessions=200, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.capacity = max_sessions
        self.clock = clock
        self.sessions: dict[UUID, Conversation] = {}
        self.guard = Lock()

    def acquire(self, session_id: UUID | None):
        with self.guard:
            now = self.clock()
            for key, conversation in list(self.sessions.items()):
                if now - conversation.touched >= self.ttl and not conversation.lock.locked():
                    del self.sessions[key]
            if session_id is None:
                if len(self.sessions) >= self.capacity:
                    raise ChatBusy("Conversation capacity reached. Try again later.")
                session_id = uuid4()
                self.sessions[session_id] = Conversation(touched=now)
            conversation = self.sessions.get(session_id)
            if conversation is None:
                raise ChatExpired("This conversation expired or the server restarted. Start a new chat.")
            if not conversation.lock.acquire(blocking=False):
                raise ChatBusy("Please wait for the current answer before sending another message.")
            conversation.touched = now
            return session_id, conversation

    def delete(self, session_id: UUID):
        with self.guard:
            conversation = self.sessions.get(session_id)
            if conversation and conversation.lock.locked():
                raise ChatBusy("Wait for the current answer before starting a new chat.")
            self.sessions.pop(session_id, None)

    def reply(self, request: ChatRequest, answer_question: Callable[[Question], Answer]) -> ChatResponse:
        session_id, conversation = self.acquire(request.session_id)
        try:
            message = request.message
            products = infer_products(message)
            normalized = normalize_question(message)

            def respond(question: Question):
                # Clarification performs no knowledge-store or Ollama calls.
                answer = clarify_scope(question) or answer_question(question)
                if question.product is not None:
                    conversation.product = question.product
                if answer.status == "clarification_required":
                    conversation.pending = question
                    conversation.pending_kind = answer.clarification_kind or "product"
                else:
                    conversation.pending = None
                    conversation.pending_kind = None
                    conversation.variant = question.variant
                    conversation.last_question = question.question
                return answer

            def fresh_question():
                explicit = next(iter(products)) if len(products) == 1 else None
                product = explicit or (conversation.product if not products else None)
                variant = infer_variant(message, product)
                if variant is None and product == conversation.product and not re.search(r"\bcompare\b", normalized):
                    variant = conversation.variant
                query = message
                if (not products and conversation.last_question
                        and re.match(r"^(?:what about|how about|does it|is it|and |why\b|what if)", normalized)):
                    prefix = "Previous question: " + conversation.last_question[:600] + "\nFollow-up question: "
                    if len(prefix) + len(message) <= 2000:
                        query = prefix + message
                return respond(Question(question=query, product=product, variant=variant))

            if normalized in {"hi", "hello", "hey", "good morning", "good evening"}:
                answer = Answer(status="clarification_required",
                    answer="Hi! What would you like to know about car, home or business insurance?")
            elif product_reply(message, products):
                product = next(iter(products))
                if conversation.pending and (conversation.pending_kind == "product" or product == conversation.product):
                    original = conversation.pending
                    question = original.model_copy(update={"product": product,
                        "variant": original.variant or infer_variant(original.question, product), "category": None})
                    answer = respond(question)
                else:
                    conversation.product = product
                    conversation.variant = None
                    conversation.pending = None
                    conversation.pending_kind = None
                    conversation.last_question = None
                    answer = Answer(status="clarification_required",
                        answer=f"What would you like to know about {product} insurance?")
            elif len(message) < 3:
                answer = Answer(status="clarification_required", answer="Please enter an insurance question.")
            elif conversation.pending:
                new_question = ((len(normalized.split()) >= 6 or
                                 (conversation.pending_kind == "product" and products and len(normalized.split()) >= 4))
                                and re.match(r"^(?:what|how|does|do|can|is|are|where|when|which|why)\b", normalized))
                changed_product = len(products) == 1 and conversation.product is not None and next(iter(products)) != conversation.product
                if new_question or changed_product:
                    answer = fresh_question()
                elif conversation.pending_kind != "product":
                    resolved = clarified_request(conversation.pending, conversation.pending_kind, message)
                    if resolved:
                        answer = respond(resolved)
                    else:
                        answer = clarify_scope(conversation.pending) or Answer(
                            status="clarification_required", answer="Please add a little more detail to your original question.")
                        if conversation.pending_kind == "incident" and normalized == "something else":
                            answer = Answer(status="clarification_required", clarification_kind="incident",
                                answer="Please describe what happened to the car, so I can narrow the search.")
                elif len(products) == 1:
                    product = next(iter(products))
                    original = conversation.pending
                    answer = respond(original.model_copy(update={"product": product}))
                else:
                    answer = clarify_scope(conversation.pending)
            else:
                answer = fresh_question()
            return ChatResponse(**answer.model_dump(), session_id=session_id, product=conversation.product)
        finally:
            conversation.touched = self.clock()
            conversation.lock.release()
