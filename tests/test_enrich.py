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


# --- Merge semantics ----------------------------------------------------------
def _make_product(**variant_kwargs):
    base = dict(
        id="X1", title="T", link="https://x", price=None, sale_price=None,
        availability=None, condition=None, color=None, size=None, material=None,
        gtin=None, mpn=None, image_link=None, additional_image_links=[],
    )
    base.update(variant_kwargs)
    return Product(
        group_id="g", title="T", description=None, brand=None,
        product_type=None, google_product_category=None, variants=[Variant(**base)],
    )


def test_apply_enrichment_fills_gaps_only():
    # Enrichment fills a null colour but must NOT overwrite a colour already set.
    product = _make_product(color=None)
    enrich.apply_enrichment(product, {"color": "Khaki", "material": None,
                                      "size": None, "specifications": {"a": "b"},
                                      "full_description": "desc"})
    assert product.variants[0].color == "Khaki"
    assert product.specifications == {"a": "b"}
    assert product.full_description == "desc"


def test_apply_enrichment_does_not_overwrite_existing_variant_data():
    product = _make_product(color="Feed Colour")
    enrich.apply_enrichment(product, {"color": "Page Colour", "material": None,
                                      "size": None, "specifications": None,
                                      "full_description": None})
    assert product.variants[0].color == "Feed Colour"


def test_enriched_product_to_dict_includes_new_fields():
    product = _make_product()
    enrich.apply_enrichment(product, {"color": "Khaki", "material": None,
                                      "size": None, "specifications": {"Färg": "Khaki"},
                                      "full_description": "desc"})
    d = product.to_dict()
    assert d["specifications"] == {"Färg": "Khaki"}
    assert d["full_description"] == "desc"


def test_unenriched_product_to_dict_omits_new_fields():
    # A product that was never enriched keeps its original shape.
    product = _make_product()
    d = product.to_dict()
    assert "specifications" not in d
    assert "full_description" not in d
