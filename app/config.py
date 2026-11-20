from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=ROOT / ".env", extra="ignore")
    data_dir: Path = ROOT / "data"
    sitemap_url: str = "https://www.allianz.com.au/sitemap.xml"
    user_agent: str = Field(default="aal_seo", min_length=1, pattern=r"^[^\r\n]+$")
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen3:4b-instruct"
    embedding_model: str = "nomic-embed-text:latest"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    collection: str = "allianz_v1"
    api_key: str | None = None
    document_signing_key: str | None = None
    document_link_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    upload_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    timeout_seconds: float = Field(default=180, gt=0)
    llm_timeout_seconds: float = Field(default=600, gt=0)
    exact_faq_lookup: bool = True
    crawl_delay_seconds: float = Field(default=1, ge=0.5)
    chunk_chars: int = Field(default=2400, ge=500, le=6000)
    retrieve_k: int = Field(default=8, ge=1, le=20)
    model_context_tokens: int = Field(default=4096, ge=4096)
    prompt_chars: int = Field(default=3000, ge=2000, le=28000)
    min_similarity: float = Field(default=0.35, ge=0, le=1)
    max_source_age_hours: int = Field(default=168, ge=1)
