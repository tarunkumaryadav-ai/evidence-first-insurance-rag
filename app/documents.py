"""Private PDF ingestion and short-lived, tamper-resistant source links."""
import hashlib
import hmac
import html
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.models import Chunk

PDF_EXTRACTOR_VERSION = "pdf-v1"


def document_secret(settings) -> str:
    secret = settings.document_signing_key
    if secret:
        return secret
    # Local-only convenience. Production must configure a stable secret.
    if not hasattr(settings, "_runtime_document_secret"):
        object.__setattr__(settings, "_runtime_document_secret", secrets.token_urlsafe(48))
    return settings._runtime_document_secret


def signature(settings, document_id: str, chunk_id: str, page: int, expires: int) -> str:
    payload = f"{document_id}:{chunk_id}:{page}:{expires}".encode()
    return hmac.new(document_secret(settings).encode(), payload, hashlib.sha256).hexdigest()


def signed_evidence_url(settings, metadata: dict, chunk_id: str) -> str:
    expires = int(time.time()) + settings.document_link_ttl_seconds
    document_id = metadata["document_id"]
    page = int(metadata["page_number"])
    query = urlencode({"chunk": chunk_id, "page": page, "expires": expires,
                       "signature": signature(settings, document_id, chunk_id, page, expires)})
    return f"/documents/{document_id}/evidence?{query}"


def verify_signature(settings, document_id: str, chunk_id: str, page: int,
                     expires: int, supplied: str) -> None:
    if expires < int(time.time()):
        raise ValueError("This source link has expired")
    expected = signature(settings, document_id, chunk_id, page, expires)
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("Invalid source link")


def safe_filename(value: str) -> str:
    name = Path(value or "document.pdf").name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")[:120]
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def extract_pdf_chunks(data: bytes, *, document_id: str, filename: str, product: str,
                       max_chars: int, fetched_at: str | None = None) -> list[Chunk]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support requires pypdf. Install the locked project dependencies.") from exc
    from io import BytesIO
    try:
        reader = PdfReader(BytesIO(data), strict=True)
    except Exception as exc:
        raise ValueError("The uploaded file is not a readable PDF") from exc
    if reader.is_encrypted:
        raise ValueError("Password-protected PDFs are not supported")
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat()
    checksum = hashlib.sha256(data).hexdigest()
    chunks: list[Chunk] = []
    for page_number, page in enumerate(reader.pages, 1):
        text = "\n".join(line.strip() for line in (page.extract_text() or "").splitlines() if line.strip())
        if not text:
            continue
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text) if part.strip()]
        groups, current, size = [], [], 0
        for paragraph in paragraphs:
            if len(paragraph) > max_chars:
                paragraph = paragraph[:max_chars]
            if current and size + len(paragraph) + 2 > max_chars:
                groups.append(current)
                current, size = [], 0
            current.append(paragraph)
            size += len(paragraph) + 2
        if current:
            groups.append(current)
        for group_index, group in enumerate(groups):
            body = "\n\n".join(group)
            section_id = str(uuid5(NAMESPACE_URL, f"document:{document_id}:page:{page_number}:group:{group_index}"))
            chunk_id = str(uuid5(NAMESPACE_URL, f"{section_id}:{checksum}:{PDF_EXTRACTOR_VERSION}"))
            chunks.append(Chunk(id=chunk_id, text=body, metadata={
                "source_url": f"document://{document_id}", "document_id": document_id,
                "document_filename": filename, "page_number": page_number,
                "product": product, "sub_product": None, "variant": None,
                "scope": "product", "applicable_products": [product], "category": "general",
                "heading_path": [filename, f"Page {page_number}"], "page_title": filename,
                "sitemap_lastmod": None, "fetched_at": timestamp, "content_hash": checksum,
                "section_id": section_id, "extractor_version": PDF_EXTRACTOR_VERSION,
                "origin": "uploaded_pdf",
            }))
    if not chunks:
        raise ValueError("No selectable text was found. This PDF may require OCR.")
    return chunks


def evidence_html(filename: str, page: int, excerpt: str, pdf_url: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Evidence - {html.escape(filename)}</title><style>
body{{margin:0;font:16px system-ui;background:#eef2f7;color:#172033}}header{{padding:14px 20px;background:#0b1830;color:white}}
main{{display:grid;grid-template-columns:minmax(280px,36%) 1fr;height:calc(100vh - 54px)}}
.evidence{{padding:22px;overflow:auto}}mark{{display:block;background:#fff19d;padding:16px;white-space:pre-wrap;line-height:1.55;border-radius:8px}}
iframe{{width:100%;height:100%;border:0;background:white}}@media(max-width:800px){{main{{display:block;height:auto}}iframe{{height:70vh}}}}
</style></head><body><header>{html.escape(filename)} - cited page {page}</header><main>
<section class='evidence'><h1>Answer evidence</h1><p>The passage used by the answer:</p><mark>{html.escape(excerpt)}</mark></section>
<iframe title='PDF page {page}' src='{html.escape(pdf_url)}#page={page}'></iframe>
</main></body></html>"""
