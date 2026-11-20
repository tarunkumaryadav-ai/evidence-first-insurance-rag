import pytest
from defusedxml.common import DefusedXmlException

from app.crawl import parse_sitemap, product_for_url, safe_url
from app.extract import extract_chunks, variant_for_url
from app.models import SitemapEntry

URL = "https://www.allianz.com.au/car-insurance/comprehensive.html"


@pytest.mark.parametrize("url", ["http://www.allianz.com.au/", "https://evil.com/",
    "https://www.allianz.com.au.evil.com/", "https://www.allianz.com.au:9999/",
    "https://user@www.allianz.com.au/", "https://localhost/"])
def test_reject_other_origins(url):
    with pytest.raises(ValueError):
        safe_url(url)


def test_sitemap_preserves_lastmod_and_missing_values():
    children, entries = parse_sitemap(b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://www.allianz.com.au/car-insurance.html</loc><lastmod>2026-01-02</lastmod></url>
    <url><loc>https://www.allianz.com.au/home-insurance.html</loc></url></urlset>''')
    assert not children
    assert entries[0].lastmod == "2026-01-02"
    assert entries[1].lastmod is None


def test_sitemap_index():
    children, entries = parse_sitemap(b'''<sitemapindex><sitemap>
    <loc>https://www.allianz.com.au/child.xml</loc></sitemap></sitemapindex>''')
    assert children == ["https://www.allianz.com.au/child.xml"] and not entries


def test_xml_entities_forbidden():
    with pytest.raises(DefusedXmlException):
        parse_sitemap(b'<!DOCTYPE x [<!ENTITY x "unsafe">]><urlset>&x;</urlset>')


def test_product_boundaries():
    assert product_for_url("https://www.allianz.com.au/business-insurance.html") == "business"
    assert product_for_url("https://www.allianz.com.au/car-insurance-fake.html") is None


def test_chunking_preserves_structure_and_conditions(html):
    chunks = extract_chunks(html, SitemapEntry(url=URL, lastmod="2026-01-02"))
    cover = next(c for c in chunks if c.metadata["heading_path"][-1] == "Cover")
    assert "wear and tear" in cover.text and "accidental damage" in cover.text
    assert cover.metadata["product"] == "car"
    assert cover.metadata["variant"] == "comprehensive"
    assert cover.metadata["sitemap_lastmod"] == "2026-01-02"
    table = next(c for c in chunks if "table" in c.metadata["html_tags"])
    assert "Feature | Example plan" in table.text
    assert "Glass | Optional, conditions apply" in table.text
    assert all("navigation offer" not in c.text for c in chunks)
    assert [c.id for c in chunks] == [c.id for c in extract_chunks(html, SitemapEntry(url=URL))]


def test_refuse_missing_main():
    with pytest.raises(ValueError, match="main"):
        extract_chunks(b"<body>Access denied</body>", SitemapEntry(url=URL))


def test_oversized_atomic_block_fails_instead_of_silent_truncation():
    with pytest.raises(ValueError, match="Oversized"):
        extract_chunks(b"<main><ul><li>" + b"x" * 13000 + b"</li></ul></main>", SitemapEntry(url=URL))


def test_global_requires_explicit_valid_products(html):
    with pytest.raises(ValueError, match="explicit"):
        extract_chunks(html, SitemapEntry(url=URL), global_products=[])


def test_essentials_variant_is_distinct_and_query_does_not_classify_product():
    assert variant_for_url("https://www.allianz.com.au/car-insurance/comprehensive-essentials.html") == "comprehensive-essentials"
    assert variant_for_url("https://www.allianz.com.au/car-insurance/comprehensive-car-insurance/essentials.html") == "comprehensive-essentials"
    assert variant_for_url("https://www.allianz.com.au/car-insurance.html?campaign=comprehensive") is None


def test_comparison_preserves_explicit_accessibility_status_labels():
    html = b'''<main><h2>Comparison</h2><table><tr><th>Feature</th><th>A</th><th>B</th></tr>
    <tr><th>Example feature</th><td><span role="img" aria-label="Included"></span></td>
    <td><span role="img" aria-label="Not included"></span></td></tr></table></main>'''
    text = extract_chunks(html, SitemapEntry(url=URL))[0].text
    assert "Example feature | Included | Not included" in text


@pytest.mark.parametrize("path, variant", [("building", "building"), ("contents", "contents"),
                                         ("home-and-contents", "home-and-contents")])
def test_home_variant_paths_seen_in_real_sitemap(path, variant):
    assert variant_for_url(f"https://www.allianz.com.au/home-insurance/{path}.html") == variant
