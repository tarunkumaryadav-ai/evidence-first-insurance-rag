"""Bounded same-origin crawler with robots checks and safe XML parsing."""
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml.ElementTree import fromstring
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings
from app.models import SitemapEntry

ORIGIN = "https://www.allianz.com.au"
MAX_BYTES = 12_000_000


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "www.allianz.com.au"
            or parts.port not in (None, 443) or parts.username or parts.password):
        raise ValueError(f"Source is outside the allowed HTTPS origin: {url}")
    return urlunsplit(("https", "www.allianz.com.au", parts.path or "/", parts.query, ""))


def product_for_url(url: str) -> str | None:
    path = urlsplit(safe_url(url)).path.lower()
    for product in ("car", "home", "business"):
        base = f"/{product}-insurance"
        if path in (base, base + ".html", base + "/") or path.startswith(base + "/"):
            return product
    return None


def parse_sitemap(content: bytes) -> tuple[list[str], list[SitemapEntry]]:
    root = fromstring(content)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError("Expected an XML sitemap index or URL set")
    children, entries = [], []
    for item in root:
        fields = {node.tag.rsplit("}", 1)[-1]: node.text for node in item}
        if not fields.get("loc"):
            continue
        url = safe_url(fields["loc"].strip())
        if kind == "sitemapindex":
            children.append(url)
        else:
            entries.append(SitemapEntry(url=url, lastmod=fields.get("lastmod")))
    return children, entries


class Crawler:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False,
                                   headers={"User-Agent": settings.user_agent})
        self.robots = RobotFileParser()
        self.last_request = 0.0

    def __enter__(self):
        try:
            content, _ = self.fetch(ORIGIN + "/robots.txt", check_robots=False)
            self.robots.parse(content.decode("utf-8").splitlines())
        except Exception:
            self.client.close()
            raise
        return self

    def __exit__(self, *_):
        self.client.close()

    @retry(retry=retry_if_exception_type((httpx.TransportError,)),
           stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def fetch(self, url: str, *, check_robots: bool = True) -> tuple[bytes, str]:
        url = safe_url(url)
        for _ in range(6):
            if check_robots and not self.robots.can_fetch(self.settings.user_agent, url):
                raise ValueError(f"robots.txt disallows {url}")
            delay = max(self.settings.crawl_delay_seconds,
                        self.robots.crawl_delay(self.settings.user_agent) or 0)
            time.sleep(max(0, delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            with self.client.stream("GET", url) as response:
                if response.is_redirect:
                    url = safe_url(urljoin(url, response.headers["location"]))
                    continue
                response.raise_for_status()
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > MAX_BYTES:
                        raise ValueError("Response exceeds maximum allowed size")
                return bytes(body), url
        raise ValueError("Too many redirects")

    def discover(self) -> list[SitemapEntry]:
        pending, visited, entries = [safe_url(self.settings.sitemap_url)], set(), {}
        while pending:
            url = pending.pop()
            if url in visited:
                continue
            if len(visited) >= 100:
                raise ValueError("Sitemap count exceeded safety bound; discovery incomplete")
            visited.add(url)
            content, _ = self.fetch(url)
            children, urls = parse_sitemap(content)
            pending.extend(children)
            for entry in urls:
                if product_for_url(entry.url):
                    entries[entry.url] = entry
        if not entries:
            raise ValueError("Sitemap contains no supported product URLs")
        return sorted(entries.values(), key=lambda entry: entry.url)
