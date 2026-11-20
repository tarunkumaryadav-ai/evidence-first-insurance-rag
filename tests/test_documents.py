from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import Settings
from app.documents import (evidence_html, extract_pdf_chunks, signed_evidence_url,
                           verify_signature)


def text_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject(
        {NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("ascii"))
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_extraction_preserves_page_and_document_metadata():
    chunks = extract_pdf_chunks(text_pdf("Storm damage is covered subject to policy terms."),
        document_id="abc123", filename="policy.pdf", product="home", max_chars=2400,
        fetched_at="2026-01-01T00:00:00+00:00")
    assert len(chunks) == 1
    assert "Storm damage" in chunks[0].text
    assert chunks[0].metadata["page_number"] == 1
    assert chunks[0].metadata["document_id"] == "abc123"
    assert chunks[0].metadata["origin"] == "uploaded_pdf"


def test_signed_source_is_tamper_resistant():
    settings = Settings(document_signing_key="x" * 48, document_link_ttl_seconds=900, _env_file=None)
    metadata = {"document_id": "doc1", "page_number": 4}
    url = signed_evidence_url(settings, metadata, "chunk1")
    query = parse_qs(urlsplit(url).query)
    verify_signature(settings, "doc1", "chunk1", 4, int(query["expires"][0]), query["signature"][0])
    with pytest.raises(ValueError, match="Invalid"):
        verify_signature(settings, "doc1", "chunk1", 5, int(query["expires"][0]), query["signature"][0])


def test_evidence_viewer_escapes_untrusted_pdf_text():
    rendered = evidence_html("<policy>.pdf", 2, "<script>alert(1)</script>", "/safe.pdf")
    assert "&lt;script&gt;" in rendered and "<script>alert" not in rendered
    assert "<mark>" in rendered and "#page=2" in rendered


def test_private_upload_list_view_and_delete(monkeypatch, settings, store):
    import app.api as api
    settings.api_key = "test-key"
    settings.document_signing_key = "s" * 48
    monkeypatch.setattr(api, "Store", lambda _: store)
    monkeypatch.setattr(api, "verify_embedding_model", lambda _: None)
    monkeypatch.setattr(api, "build_graph", lambda *_: None)
    monkeypatch.setattr(store, "close", lambda: None)
    headers = {"X-API-Key": "test-key", "Content-Type": "application/pdf"}
    data = text_pdf("Storm damage is covered subject to policy terms.")
    with TestClient(api.create_app(settings)) as client:
        uploaded = client.post("/documents?product=home&filename=policy.pdf",
                               headers=headers, content=data)
        assert uploaded.status_code == 200
        document_id = uploaded.json()["id"]
        assert client.get("/documents", headers={"X-API-Key": "test-key"}).json()[0]["id"] == document_id
        with store.connect() as db:
            row = db.execute("SELECT id,body,metadata FROM chunks WHERE url=?",
                             (f"document://{document_id}",)).fetchone()
        import json
        metadata = json.loads(row["metadata"])
        start = row["body"].index("Storm damage")
        end = start + len("Storm damage is covered subject to policy terms.")
        source = signed_evidence_url(settings, metadata, row["id"], start, end)
        viewer = client.get(source)
        assert viewer.status_code == 200 and (
            "<mark>Storm damage is covered subject to policy terms.</mark>" in viewer.text)
        assert "cited page 1" in viewer.text
        assert client.get(source.replace("page=1", "page=2")).status_code == 403
        assert client.get(source.replace(f"start={start}", f"start={start + 1}")).status_code == 403
        assert client.delete(f"/documents/{document_id}", headers={"X-API-Key": "test-key"}).status_code == 200
        assert client.get("/documents", headers={"X-API-Key": "test-key"}).json() == []
