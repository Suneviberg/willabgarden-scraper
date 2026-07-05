"""Tests for page-level enrichment (enrich.py).

These run fully offline against saved product-page fixtures in
``tests/fixtures/`` — no network access. The fixtures are real, unmodified pages
captured from the live site.

Run with: ``pytest``
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import enrich  # noqa: E402
from scraper import Product, Variant  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


# --- Blob extraction ----------------------------------------------------------
def test_extract_product_json_returns_product():
    product = enrich.extract_product_json(load_fixture("tulip_solsang"))
    assert product is not None
    assert product["name"] == "Tulip solsäng"
    assert "additionalDetails" in product


def test_extract_returns_none_for_non_product_html():
    assert enrich.extract_product_json("<html><body>no blob here</body></html>") is None


# --- Spec-table parsing -------------------------------------------------------
def test_parse_spec_table_strong_colon_br():
    body = "<strong>Artikelnummer</strong>: 8003425K<br>\n<strong>Färg</strong>: Khaki<br>"
    assert enrich.parse_spec_table(body) == {
        "Artikelnummer": "8003425K",
        "Färg": "Khaki",
    }


def test_parse_spec_table_handles_invalid_br_and_trailing_colon_in_key():
    # The "Teknisk specifikation" section uses </br> and colons inside <strong>.
    body = "<strong>Tillverkning:</strong>  Kan tillverkas </br><strong>Garanti:</strong>  5 år </br>"
    assert enrich.parse_spec_table(body) == {
        "Tillverkning": "Kan tillverkas",
        "Garanti": "5 år",
    }


# --- End-to-end enrichment on real fixtures -----------------------------------
def test_enrich_tulip_extracts_colour_and_specs():
    e = enrich.enrich_from_html(load_fixture("tulip_solsang"))
    assert e["color"] == "Khaki"
    assert e["specifications"]["Varumärke"] == "Garden Living"
    assert e["full_description"]  # rich description present


def test_enrich_wg40_extracts_colour_and_full_spec_table():
    e = enrich.enrich_from_html(load_fixture("wg40_skjutdorr"))
    assert e["color"] == "Vit RAL 9010"
    specs = e["specifications"]
    # Both the product and technical spec sections are merged.
    assert specs["Höjd"] == "2000 mm"
    assert specs["Typ av glas"] == "4 mm säkerhetsenergiglas"


# --- Merge semantics (per variant) -------------------------------------------
def _make_variant(**kwargs):
    base = dict(
        id="X1", title="T", link="https://x", price=None, sale_price=None,
        availability=None, condition=None, color=None, size=None, material=None,
        gtin=None, mpn=None, image_link=None, additional_image_links=[],
    )
    base.update(kwargs)
    return Variant(**base)


def _make_product(variants):
    return Product(
        group_id="g", title="T", description=None, brand=None,
        product_type=None, google_product_category=None, variants=variants,
    )


def test_apply_variant_enrichment_fills_gaps_only():
    v = _make_variant(color=None)
    enrich.apply_variant_enrichment(v, {"color": "Khaki", "material": None,
                                        "size": None, "specifications": {"a": "b"}})
    assert v.color == "Khaki"
    assert v.specifications == {"a": "b"}


def test_apply_variant_enrichment_does_not_overwrite_feed_data():
    v = _make_variant(color="Feed Colour")
    enrich.apply_variant_enrichment(v, {"color": "Page Colour", "material": None,
                                        "size": None, "specifications": None})
    assert v.color == "Feed Colour"


def test_variants_get_their_own_colour():
    # The core correctness property: two variants of one product enriched from
    # their own pages must keep DISTINCT colours (not share the first one).
    v1 = _make_variant(id="A")
    v2 = _make_variant(id="B")
    enrich.apply_variant_enrichment(v1, {"color": "RAL 9010 - Vit", "material": None,
                                         "size": None, "specifications": None})
    enrich.apply_variant_enrichment(v2, {"color": "RAL 9008 - Metallicgrå",
                                         "material": None, "size": None,
                                         "specifications": None})
    assert v1.color == "RAL 9010 - Vit"
    assert v2.color == "RAL 9008 - Metallicgrå"


def test_enriched_variant_to_dict_includes_specifications():
    v = _make_variant()
    enrich.apply_variant_enrichment(v, {"color": "Khaki", "material": None,
                                        "size": None,
                                        "specifications": {"Färg": "Khaki"}})
    d = v.to_dict()
    assert d["specifications"] == {"Färg": "Khaki"}


def test_unenriched_output_omits_new_fields():
    v = _make_variant()
    product = _make_product([v])
    assert "specifications" not in v.to_dict()
    assert "full_description" not in product.to_dict()
