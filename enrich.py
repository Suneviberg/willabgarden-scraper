#!/usr/bin/env python3
"""Page-level enrichment for the Willab Garden scraper.

The Google Merchant feed (see ``scraper.py``) is complete and reliable but omits
structured attributes such as colour, material, dimensions, and the full spec
table. That information *is* available on each product page — not in the
server-rendered HTML (the page is client-side rendered and ships an empty
``<main>``), but embedded in a JSON hydration blob under ``pageContent.product``.

This module extracts that blob **without a headless browser** and turns it into
extra fields that are merged onto the feed's variants:

- ``color`` / ``material`` / ``size`` — filled from the product's
  "Produktspecifikation" table when the feed left them ``null``.
- ``specifications`` — the full spec table as a key/value dict.
- ``full_description`` — the page's rich description text.

Enrichment happens at the **product level** (one request per product, keyed by
``item_group_id``) rather than per variant, because the spec table is
product-level. It is opt-in via ``scraper.py --enrich`` so the core scraper
stays fast and dependency-light.

The ``pageContent.product`` shape is an internal CMS structure and could change;
enrichment is therefore best-effort and never fails the run — a page that can't
be parsed simply leaves the feed data untouched.
"""

from __future__ import annotations

import html as html_module
import json
import re
import time
from typing import Optional

import requests

from scraper import USER_AGENT, http_get


# --- Extracting the hydration blob -------------------------------------------
def extract_product_json(html: str) -> Optional[dict]:
    """Return the ``pageContent.product`` object embedded in a product page.

    The page embeds its server state as JSON inside the largest ``<script>``
    element. Depending on the template that JSON is either present directly or
    as an escaped string literal, so we try both. Returns ``None`` if no product
    object is found (e.g. a non-product page).
    """
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S | re.I)
    scripts.sort(key=len, reverse=True)
    for script in scripts[:3]:  # the product state is always the biggest blob
        start, end = script.find("{"), script.rfind("}")
        if start < 0 or end < 0:
            continue
        raw = script[start : end + 1]
        data = _loads_maybe_escaped(raw)
        if isinstance(data, dict) and "pageContent" in data:
            page = data["pageContent"]
            if isinstance(page, dict):
                return page.get("product")
    return None


def _loads_maybe_escaped(raw: str) -> Optional[object]:
    """Parse ``raw`` as JSON, retrying once if it's an escaped string literal."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        unescaped = raw.encode().decode("unicode_escape").encode("latin1").decode(
            "utf-8"
        )
        return json.loads(unescaped)
    except (json.JSONDecodeError, UnicodeError):
        return None


# --- Parsing the product object ----------------------------------------------
# Spec tables render each row as "<strong>Key</strong>: Value" separated by a
# line break. The break is written inconsistently across sections — <br>, <br/>,
# and even the invalid </br> all appear — so the row terminator matches any of
# them (or the next <strong>, or end of string).
_SPEC_ROW_RE = re.compile(
    r"<strong>\s*(?P<key>.*?)\s*</strong>"
    r"\s*:?\s*"
    r"(?P<value>.*?)"
    r"\s*(?:<\s*/?\s*br\s*/?\s*>|(?=<strong>)|$)",
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Strip HTML tags and normalise whitespace/entities in a fragment."""
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_spec_table(body: str) -> dict:
    """Turn a "<strong>Key</strong>: Value<br>" block into a ``{key: value}`` dict.

    Keys may carry a trailing colon in the source (``Tillverkning:``); it is
    stripped so keys are consistent.
    """
    specs: dict = {}
    for m in _SPEC_ROW_RE.finditer(body or ""):
        key = _clean(m.group("key")).rstrip(":").strip()
        value = _clean(m.group("value"))
        if key and value:
            specs[key] = value
    return specs


# Map Swedish spec-table keys to our variant fields. Values are matched
# case-insensitively; the first present key wins.
_COLOR_KEYS = ("färg", "kulör")
_MATERIAL_KEYS = ("material", "materialtyp")
_SIZE_KEYS = ("yta", "storlek", "mått", "dimension")


def _first_key(specs: dict, wanted: tuple) -> Optional[str]:
    """First spec value whose key exactly matches one of ``wanted``."""
    lower = {k.lower(): v for k, v in specs.items()}
    for key in wanted:
        if key in lower:
            return lower[key]
    return None


def _first_key_prefix(specs: dict, prefixes: tuple) -> Optional[str]:
    """First spec value whose key *starts with* one of ``prefixes``.

    Materials appear under several keys (``Material väggar``, ``Material tak``);
    this catches the first while the full set stays in ``specifications``.
    """
    for k, v in specs.items():
        kl = k.lower()
        if any(kl.startswith(p) for p in prefixes):
            return v
    return None


def build_enrichment(product: dict) -> dict:
    """Extract the useful extra fields from a ``pageContent.product`` object."""
    details = product.get("additionalDetails") or []
    specs: dict = {}
    for section in details:
        if not isinstance(section, dict):
            continue
        title = (section.get("title") or "").lower()
        if "specifikation" in title:  # Produktspecifikation + Teknisk specifikation
            specs.update(parse_spec_table(section.get("body", "")))

    body = product.get("details")
    full_description = None
    if isinstance(body, dict):
        full_description = _clean(body.get("body", "")) or None

    return {
        "color": _first_key(specs, _COLOR_KEYS),
        "material": _first_key_prefix(specs, _MATERIAL_KEYS),
        "size": _first_key(specs, _SIZE_KEYS),
        "specifications": specs or None,
        "full_description": full_description,
    }


def enrich_from_html(html: str) -> Optional[dict]:
    """Full pipeline: page HTML → enrichment dict (or ``None`` if not a product)."""
    product = extract_product_json(html)
    if product is None:
        return None
    return build_enrichment(product)


# --- Applying enrichment to feed products ------------------------------------
def apply_enrichment(product, enrichment: dict) -> None:
    """Merge an enrichment dict onto a feed ``Product`` in place.

    Feed data wins where it exists; enrichment only *fills gaps* for
    color/material/size, and adds the new ``specifications`` /
    ``full_description`` fields at the product level. The enrichment reflects the
    product's default variant, so the attribute fills are applied to variants
    that are still missing them.
    """
    product.specifications = enrichment.get("specifications")
    product.full_description = enrichment.get("full_description")
    for variant in product.variants:
        for field_name in ("color", "material", "size"):
            if getattr(variant, field_name) is None and enrichment.get(field_name):
                setattr(variant, field_name, enrichment[field_name])


def enrich_products(
    products,
    session: Optional[requests.Session] = None,
    delay: float = 0.3,
    limit: Optional[int] = None,
    progress: bool = True,
) -> dict:
    """Enrich a list of feed ``Product`` objects by fetching each product page.

    One request per product (using the first variant's ``link``), throttled by
    ``delay`` seconds to stay polite. Returns a small stats dict. Never raises on
    a single-page failure — enrichment is best-effort.
    """
    session = session or requests.Session()
    targets = products[:limit] if limit is not None else products
    stats = {"attempted": 0, "enriched": 0, "failed": 0, "skipped_no_link": 0}

    for i, product in enumerate(targets, 1):
        link = product.variants[0].link if product.variants else None
        if not link:
            stats["skipped_no_link"] += 1
            continue
        stats["attempted"] += 1
        try:
            html = http_get(link, session=session).decode("utf-8", errors="replace")
            enrichment = enrich_from_html(html)
            if enrichment is None:
                stats["failed"] += 1
            else:
                apply_enrichment(product, enrichment)
                stats["enriched"] += 1
        except Exception:  # network/parse issue on one page must not abort the run
            stats["failed"] += 1

        if progress and (i % 25 == 0 or i == len(targets)):
            print(
                f"  enriched {i}/{len(targets)} products "
                f"({stats['enriched']} ok, {stats['failed']} failed)"
            )
        if delay:
            time.sleep(delay)

    return stats
