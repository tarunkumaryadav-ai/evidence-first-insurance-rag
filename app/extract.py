"""Heading/HTML based extraction. No LLM-generated ingestion metadata."""
import hashlib
import re
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawl import product_for_url, safe_url
from app.models import Chunk, SitemapEntry

EXTRACTOR_VERSION = "html-v3"
VARIANTS = {
    "car": {"comprehensive-essentials": ["comprehensive-essentials",
                                           "comprehensive-car-insurance/essentials.html"],
            "comprehensive": ["comprehensive"], "third-party": ["third-party", "third party"]},
    "home": {"home-and-contents": ["/home-insurance/home-and-contents.html"],
             "building": ["building-insurance", "buildings-insurance", "/home-insurance/building.html"],
             "contents": ["contents-insurance", "/home-insurance/contents.html"],
             "landlord": ["landlord"]},
    "business": {},
}


def variant_for_url(url: str) -> str | None:
    product = product_for_url(url)
    return next((name for name, matches in VARIANTS.get(product, {}).items()
                 if any(match in urlsplit(url).path.lower() for match in matches)), None)


def category_for(heading: str) -> str:
    for category, terms in {
        "exclusions": ("exclusion", "not covered", "don't cover", "not cover"),
        "claims": ("claim",), "eligibility": ("eligib",),
        "coverage": ("cover", "benefit", "inclusion"),
        "pricing": ("cost", "premium", "discount"),
        "faq": ("frequently", "question"),
    }.items():
        if any(term in heading.lower() for term in terms):
            return category
    return "general"


def extract_chunks(html: bytes, entry: SitemapEntry, max_chars: int = 2400,
                   fetched_at: str | None = None, *, global_products: list[str] | None = None,
                   sub_product: str | None = None, variant: str | None = None) -> list[Chunk]:
    url = safe_url(entry.url)
    product = product_for_url(url)
    if global_products is not None:
        if not global_products or not set(global_products) <= {"car", "home", "business"}:
            raise ValueError("Global content requires explicit supported applicable products")
        product = "global"
    if not product:
        raise ValueError("Page is outside the supported product paths")
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("main") or soup.find(attrs={"role": "main"})
    if root is None:
        raise ValueError("No main content region; refusing to index the entire page")
    for node in root.select("script,style,nav,footer,header,noscript,form,[aria-hidden='true']"):
        node.decompose()
    # Allianz comparison status icons carry their meaning in accessibility text.
    # Preserve explicit site labels; never infer coverage from a CSS class/color.
    for node in root.select("[role='img'][aria-label], img[alt]"):
        label = node.get("aria-label") or node.get("alt")
        if label and not node.get_text(strip=True):
            node.replace_with(" " + label.strip() + " ")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    sections, path, blocks = [], [], []

    def flush():
        if blocks:
            sections.append((list(path), list(blocks)))
            blocks.clear()

    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "summary"]):
        if node.find_parent(["ul", "ol", "table"]):
            continue
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if re.fullmatch(r"h[1-6]", node.name):
            flush()
            level = int(node.name[1])
            path[:] = [(rank, value) for rank, value in path if rank < level]
            path.append((level, text))
        else:
            if node.name == "table":
                text = "\n".join(" | ".join(cell.get_text(" ", strip=True)
                                  for cell in row.find_all(["th", "td"]))
                                  for row in node.find_all("tr"))
            elif node.name in ("ul", "ol"):
                text = "\n".join("- " + item.get_text(" ", strip=True)
                                 for item in node.find_all("li", recursive=False))
            blocks.append((node.name, text))
    flush()
    timestamp = fetched_at or datetime.now(timezone.utc).isoformat()
    page_hash = hashlib.sha256(html).hexdigest()
    chunks = []
    for heading_pairs, section_blocks in sections:
        headings = [value for _, value in heading_pairs]
        # Keep entire sections together where practical. Long paragraphs are split;
        # lists and tables remain atomic so labels/conditions are not detached.
        units = []
        for tag, text in section_blocks:
            if len(text) > 12000:
                raise ValueError(f"Oversized {tag} block requires an extraction rule: {url}")
            parts = [text]
            if tag == "p" and len(text) > max_chars:
                parts = re.split(r"(?<=[.!?])\s+", text)
                if any(len(part) > max_chars for part in parts):
                    raise ValueError(f"Oversized sentence requires an extraction rule: {url}")
            units.extend((tag, part) for part in parts)
        groups, current, size = [], [], 0
        for tag, text in units:
            if current and size + len(text) > max_chars:
                groups.append(current)
                current, size = [], 0
            current.append((tag, text))
            size += len(text) + 2
        if current:
            groups.append(current)
        section_id = str(uuid5(NAMESPACE_URL, url + "|" + repr(headings)))
        for group in groups:
            text = "\n\n".join(value for _, value in group)
            identity = f"{url}|{page_hash}|{section_id}|{len(chunks)}|{EXTRACTOR_VERSION}"
            chunks.append(Chunk(id=str(uuid5(NAMESPACE_URL, identity)), text=text, metadata={
                "source_url": url, "product": product, "sub_product": sub_product,
                "variant": variant or (variant_for_url(url) if product != "global" else None),
                "scope": "global" if global_products else "product",
                "applicable_products": sorted(set(global_products or [product])),
                "category": category_for(" > ".join(headings)),
                "heading_path": headings, "html_tags": sorted({tag for tag, _ in group}),
                "page_title": title, "sitemap_lastmod": entry.lastmod,
                "fetched_at": timestamp, "content_hash": page_hash,
                "section_id": section_id, "extractor_version": EXTRACTOR_VERSION,
            }))
    if not chunks:
        raise ValueError("No usable structural content extracted")
    return chunks
