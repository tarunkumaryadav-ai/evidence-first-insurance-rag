from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Product = Literal["car", "home", "business"]


class SitemapEntry(BaseModel):
    url: str
    lastmod: str | None = None


class Chunk(BaseModel):
    id: str
    text: str
    metadata: dict


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"question": "How much is Allianz car insurance?"}]})
    question: str = Field(min_length=3, max_length=2000,
                         description="Your insurance question. Mention car, home or business when known.")
    product: Product | None = Field(default=None,
        description="Optional override. Normally inferred from the question; ambiguous questions prompt clarification.")
    variant: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)

    @field_validator("question", mode="before")
    @classmethod
    def strip_question(cls, value):
        return value.strip() if isinstance(value, str) else value


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_ids: list[str] = Field(max_length=3)


class EvidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1, max_length=60)


class Source(BaseModel):
    url: str
    chunk_id: str
    heading_path: list[str]
    sitemap_lastmod: str | None
    fetched_at: str
    origin: str
    page_number: int | None = None
    document_filename: str | None = None


class Answer(BaseModel):
    status: Literal["answered", "abstained", "clarification_required"]
    answer: str
    sources: list[Source] = Field(default_factory=list)
    sources_checked: list[str] = Field(default_factory=list)
    # Discovery URL is navigation, never falsely represented as supporting evidence.
    discovery_url: str = "https://www.allianz.com.au/sitemap.xml"
    mode: Literal["extractive"] = "extractive"
    clarification_kind: Literal["product", "intent", "incident", "variant", "topic"] | None = None
    choices: list[str] = Field(default_factory=list, max_length=6)
