"""Schema and unit tests for the Willab Garden scraper.

The schema test parses a tiny in-memory feed (no network) and asserts the
grouped-output invariants. The unit tests exercise the pure parsing helpers.

Run with: ``pytest``
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402


# A minimal Atom feed with the Google Merchant namespace: one two-variant
# product (shared item_group_id) and one standalone product (no group id).
SAMPLE_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns:g="http://base.google.com/ns/1.0" xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <entry>
    <g:id>A1</g:id>
    <g:title>Magnolia loungeset 12.5 m\xc2\xb2</g:title>
    <g:description>Fin soffa i aluminium.</g:description>
    <g:link>https://example.se/a1/</g:link>
    <g:image_link>https://img/a1.jpg</g:image_link>
    <g:additional_image_link>https://img/a1-2.jpg</g:additional_image_link>
    <g:additional_image_link>https://img/a1-3.jpg</g:additional_image_link>
    <g:price>12995.00 SEK</g:price>
    <g:sale_price>10995.00 SEK</g:sale_price>
    <g:availability>in stock</g:availability>
    <g:condition>new</g:condition>
    <g:brand>Garden Living</g:brand>
    <g:product_type>Utem\xc3\xb6bler &gt; Soffgrupper</g:product_type>
    <g:item_group_id>12345</g:item_group_id>
  </entry>
  <entry>
    <g:id>A2</g:id>
    <g:title>Magnolia loungeset 15 m\xc2\xb2</g:title>
    <g:description>Fin soffa i aluminium.</g:description>
    <g:link>https://example.se/a2/</g:link>
    <g:image_link>https://img/a2.jpg</g:image_link>
    <g:price>13995,50 SEK</g:price>
    <g:availability>in stock</g:availability>
    <g:condition>new</g:condition>
    <g:brand>Garden Living</g:brand>
    <g:product_type>Utem\xc3\xb6bler &gt; Soffgrupper</g:product_type>
    <g:item_group_id>12345</g:item_group_id>
  </entry>
  <entry>
    <g:id>B1</g:id>
    <g:title>Enskild pall</g:title>
    <g:link>https://example.se/b1/</g:link>
    <g:image_link>https://img/b1.jpg</g:image_link>
    <g:price>499.00 SEK</g:price>
    <g:availability>out of stock</g:availability>
  </entry>
</feed>
"""


@pytest.fixture
def products():
    prods, count = scraper.parse_feed(io.BytesIO(SAMPLE_FEED))
    assert count == 3
    return prods


def test_grouping(products):
    # Two variants collapse into one product; the standalone becomes its own.
    assert len(products) == 2
    grouped, solo = products[0], products[1]
    assert grouped.group_id == "12345"
    assert len(grouped.variants) == 2
    assert solo.group_id is None
    assert len(solo.variants) == 1


def test_uniform_schema_every_product_has_variants(products):
    for p in products:
        d = p.to_dict()
        assert isinstance(d["variants"], list)
        assert len(d["variants"]) >= 1
        assert d["variant_count"] == len(d["variants"])


def test_every_variant_has_id_and_price(products):
    for p in products:
        for v in p.to_dict()["variants"]:
            assert v["id"]
            assert v["price"] is not None
            assert v["price"]["amount"] > 0


def test_shared_attributes_lifted_to_product(products):
    grouped = products[0]
    # Base title is the common prefix; brand/type lifted; description shared.
    assert grouped.title == "Magnolia loungeset"
    assert grouped.brand == "Garden Living"
    assert grouped.product_type == "Utemöbler > Soffgrupper"
    assert grouped.description == "Fin soffa i aluminium."


def test_multiple_images_collected(products):
    v = products[0].variants[0]
    assert v.additional_image_links == ["https://img/a1-2.jpg", "https://img/a1-3.jpg"]


def test_derived_size_and_material(products):
    v = products[0].variants[0]  # "Magnolia loungeset 12.5 m²" + aluminium in desc? no—title only
    assert v.size == "12.5 m²"
    # material derives from the title, which has no material word here
    assert v.material is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3955.50 SEK", {"amount": 3955.5, "currency": "SEK"}),
        ("57900.0000000000 SEK", {"amount": 57900.0, "currency": "SEK"}),
        ("13995,50 SEK", {"amount": 13995.5, "currency": "SEK"}),
        ("1 299,00 SEK", {"amount": 1299.0, "currency": "SEK"}),
        ("499", {"amount": 499.0, "currency": None}),
        (None, None),
        ("", None),
        ("not a price", None),
    ],
)
def test_parse_price(raw, expected):
    assert scraper.parse_price(raw) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Green Room Classic växthus 24.4 m²", "24.4 m²"),
        ("Bord 120x80 cm", "120x80 cm"),
        ("Enskild pall", None),
        (None, None),
    ],
)
def test_derive_size(title, expected):
    assert scraper.derive_size(title) == expected


def test_document_counts():
    prods, _ = scraper.parse_feed(io.BytesIO(SAMPLE_FEED))
    doc = scraper.build_document(prods, "http://example/feed")
    assert doc["product_count"] == 2
    assert doc["variant_count"] == 3
    assert doc["source"] == "http://example/feed"
    assert doc["scraped_at"].endswith("Z")
