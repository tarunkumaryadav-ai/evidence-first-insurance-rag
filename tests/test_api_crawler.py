import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.crawl import Crawler
from app.models import Answer


def crawler_with_transport(settings, handler):
    settings.crawl_delay_seconds = 0.5
    crawler = Crawler(settings)
    crawler.client.close()
    crawler.client = httpx.Client(transport=httpx.MockTransport(handler), headers=crawler.client.headers)
    return crawler


def test_crawler_checks_robots_and_preserves_redirect_boundary(settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://evil.com/"})
        raise AssertionError("Unexpected request")
    with crawler_with_transport(settings, handler) as crawler:
        with pytest.raises(ValueError, match="robots"):
            crawler.fetch("https://www.allianz.com.au/private")
        with pytest.raises(ValueError, match="origin"):
            crawler.fetch("https://www.allianz.com.au/redirect")


def test_403_is_error_not_empty_discovery(settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(403, text="Access denied")
    with crawler_with_transport(settings, handler) as crawler:
        with pytest.raises(httpx.HTTPStatusError):
            crawler.discover()


def test_configured_user_agent_used_in_requests_and_robots(settings):
    settings.user_agent = "aal_seo"
    def handler(request):
        assert request.headers["user-agent"] == "aal_seo"
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: aal_seo\nDisallow: /restricted\n\n"
                                 "User-agent: *\nAllow: /")
        return httpx.Response(200, text="allowed")
    with crawler_with_transport(settings, handler) as crawler:
        with pytest.raises(ValueError, match="robots"):
            crawler.fetch("https://www.allianz.com.au/restricted")
        assert crawler.fetch("https://www.allianz.com.au/car-insurance.html")[0] == b"allowed"


def test_api_auth_validation_and_failure(monkeypatch, settings, store):
    import app.api as api
    settings.api_key = "test-secret"
    monkeypatch.setattr(api, "Store", lambda _: store)
    monkeypatch.setattr(api, "verify_embedding_model", lambda _: None)
    class Graph:
        def invoke(self, _):
            return {"response": Answer(status="abstained", answer="No evidence available.")}
    monkeypatch.setattr(api, "build_graph", lambda *_: Graph())
    # Store is owned by the fixture; prevent TestClient lifespan from closing it twice.
    monkeypatch.setattr(store, "close", lambda: None)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.post("/ask", json={"question": "What is covered?"}).status_code == 401
        headers = {"X-API-Key": "test-secret"}
        assert client.get("/stats", headers=headers).json()["models"] == {
            "llm": settings.llm_model, "embedding": settings.embedding_model}
        assert client.post("/ask", headers=headers,
            json={"question": "What is covered?", "product": "travel"}).status_code == 422
        response = client.post("/ask", headers=headers,
            json={"question": "What is covered?", "product": "car"})
        assert response.status_code == 200
        assert response.json()["status"] == "abstained"
        assert client.get("/health/ready", headers=headers).status_code == 503
        def unavailable(_):
            raise RuntimeError("secret backend error")
        monkeypatch.setattr(api, "verify_embedding_model", unavailable)
        failed = client.post("/ask", headers=headers, json={"question": "What is covered?"})
        assert failed.status_code == 503
        assert "secret backend" not in failed.text


def test_failed_sync_is_recorded_without_deleting_content(monkeypatch, store, settings, html):
    import app.ingest as ingestion
    from app.ingest import ingest_html, sync
    from app.models import SitemapEntry
    ingest_html(store, html, SitemapEntry(url="https://www.allianz.com.au/car-insurance/test.html"))
    monkeypatch.setattr(ingestion, "verify_embedding_model", lambda _: None)
    class BlockedCrawler:
        def __init__(self, _):
            pass
        def __enter__(self):
            raise RuntimeError("403 access denied")
        def __exit__(self, *_):
            pass
    monkeypatch.setattr(ingestion, "Crawler", BlockedCrawler)
    result = sync(store, settings)
    assert result["status"] == "failed"
    assert store.stats()["pages"] == 1
    assert store.stats()["latest_run"]["status"] == "failed"
