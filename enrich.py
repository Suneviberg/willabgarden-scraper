#!/usr/bin/env python3
"""Page-level enrichment for the Willab Garden scraper.

The Google Merchant feed (see ``scraper.py``) is complete and reliable but omits
structured attributes such as colour, material, dimensions, and the full spec
table. That information *is* available on each product page — not in the
server-rendered HTML (the page is client-side rendered and ships an empty
``<main>``), but embedded in a JSON hydration blob under ``pageContent.product``.

This module extracts that blob **without a headless browser** and merges it onto
the feed's variants:

- ``color`` / ``material`` / ``size`` and a full ``specifications`` table are
  filled **per variant**, from each variant's own page.
- ``full_description`` is added at the **product level** (it is shared across a
  product's variants).

**Enrichment is per variant, not per product.** Colour, dimensions and the spec
table differ *between* the variants of one product (a greenhouse comes in a
matrix of sizes × colours, each its own SKU and page), so a single page cannot
describe its siblings — each variant's attributes live only on its own page.
Fetching one page per group and copying its colour onto every variant would be
wrong; we fetch every variant's own page instead.

Because that is up to ~13.6k requests on the full catalogue, enrichment is:

- **concurrent** — a bounded thread pool (``--enrich-workers``),
- **cached** — fetched pages are stored on disk (``--enrich-cache``) so re-runs
  are near-instant and repeat runs don't re-hit the server,
- **polite** — a small per-request delay, and
- **opt-in** — only runs with ``scraper.py --enrich``.

The ``pageContent.product`` shape is an internal CMS structure and could change;
enrichment is therefore best-effort and never fails the run — a page that can't
be fetched or parsed simply leaves that variant's feed data untouched.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

from scraper import http_get


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
    """Extract the useful extra fields from one ``pageContent.product`` object.

    This describes a *single variant* — the SKU whose page was fetched.
    """
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


# --- Applying enrichment to a single variant ---------------------------------
def apply_variant_enrichment(variant, enrichment: dict) -> None:
    """Merge one variant's page enrichment onto that ``Variant`` in place.

    Feed data wins where it exists: ``color`` / ``material`` / ``size`` are only
    filled when the feed left them ``None``. The full ``specifications`` table
    (which is variant-specific) is attached to the variant.
    """
    for field_name in ("color", "material", "size"):
        if getattr(variant, field_name) is None and enrichment.get(field_name):
            setattr(variant, field_name, enrichment[field_name])
    if enrichment.get("specifications"):
        variant.specifications = enrichment["specifications"]


# --- Disk page cache ----------------------------------------------------------
class PageCache:
    """Tiny disk cache mapping a URL to its fetched HTML.

    Keyed by a hash of the URL so re-runs (and repeated runs) never re-hit the
    server for a page already seen. A ``None`` directory disables caching.
    """

    def __init__(self, directory: Optional[str]):
        self.directory = directory
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _path(self, url: str) -> Optional[str]:
        if not self.directory:
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return os.path.join(self.directory, f"{digest}.html")

    def get(self, url: str) -> Optional[str]:
        path = self._path(url)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        return None

    def put(self, url: str, html: str) -> None:
        path = self._path(url)
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)


# --- Orchestration ------------------------------------------------------------
def enrich_products(
    products,
    workers: int = 8,
    delay: float = 0.1,
    cache_dir: Optional[str] = None,
    limit: Optional[int] = None,
    progress: bool = True,
) -> dict:
    """Enrich feed ``Product`` objects by fetching **each variant's own page**.

    Colour, dimensions and the spec table differ between a product's variants, so
    every variant is enriched from its own page. Requests run on a bounded thread
    pool (``workers``), each throttled by ``delay`` seconds, and pages are served
    from ``cache_dir`` when present. The product-level ``full_description`` is set
    from the first variant that yields one (it is shared across variants).

    Returns a stats dict. A single page failing never aborts the run.
    """
    cache = PageCache(cache_dir)
    session = requests.Session()
    throttle = threading.Lock()

    targets = products[:limit] if limit is not None else products
    # Flatten to (product, variant) tasks — one page fetch per variant.
    tasks = [
        (product, variant)
        for product in targets
        for variant in product.variants
        if variant.link
    ]
    total = len(tasks)
    stats = {"variants": total, "enriched": 0, "failed": 0, "from_cache": 0}
    done = 0
    stats_lock = threading.Lock()

    def fetch_html(url: str) -> str:
        cached = cache.get(url)
        if cached is not None:
            with stats_lock:
                stats["from_cache"] += 1
            return cached
        # Politeness: serialise the *rate* (short sleep) without serialising the
        # whole request, so workers still overlap network latency.
        if delay:
            with throttle:
                time.sleep(delay)
        html = http_get(url, session=session).decode("utf-8", errors="replace")
        cache.put(url, html)
        return html

    def work(task):
        product, variant = task
        try:
            html = fetch_html(variant.link)
            enrichment = enrich_from_html(html)
            if enrichment is None:
                return product, None
            apply_variant_enrichment(variant, enrichment)
            return product, enrichment
        except Exception:  # one page's failure must not abort the run
            return product, "error"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for product, result in pool.map(work, tasks):
            with stats_lock:
                done += 1
                if result == "error" or result is None:
                    stats["failed"] += 1
                else:
                    stats["enriched"] += 1
                    # full_description is shared; set it once per product.
                    if product.full_description is None and result.get(
                        "full_description"
                    ):
                        product.full_description = result["full_description"]
                if progress and (done % 250 == 0 or done == total):
                    print(
                        f"  enriched {done}/{total} variants "
                        f"({stats['enriched']} ok, {stats['failed']} failed, "
                        f"{stats['from_cache']} cached)"
                    )

    return stats
