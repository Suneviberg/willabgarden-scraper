#!/usr/bin/env python3
"""Willab Garden product scraper.

Reads the retailer's public Google Merchant product feed, groups variants by
their ``item_group_id``, and writes a structured JSON catalogue.

The feed is an Atom 1.0 document (``<feed>`` / ``<entry>``) whose product
fields live in the Google Merchant namespace (``g:``). Parsing is streamed with
``lxml.iterparse`` so the full ~13k-entry / ~55 MB feed never has to sit in
memory at once.

Run ``python scraper.py --help`` for options.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

import requests
from lxml import etree

# --- Namespaces ---------------------------------------------------------------
# The feed is Atom with product data in the Google Merchant namespace. lxml
# reports tags in Clark notation ("{uri}local"), so we build the qualified names
# once and reuse them.
ATOM_NS = "http://www.w3.org/2005/Atom"
G_NS = "http://base.google.com/ns/1.0"
ENTRY_TAG = f"{{{ATOM_NS}}}entry"


def g(tag: str) -> str:
    """Qualified name for a Google Merchant (``g:``) child element."""
    return f"{{{G_NS}}}{tag}"


DEFAULT_FEED_URL = "https://www.willabgarden.se/googleproductfeed"
DEFAULT_OUTPUT = "willabgarden_products.json"
USER_AGENT = "willabgarden-scraper/1.0 (interview case)"


# --- Data model ---------------------------------------------------------------
@dataclass
class Variant:
    id: Optional[str]
    title: Optional[str]
    link: Optional[str]
    price: Optional[dict]
    sale_price: Optional[dict]
    availability: Optional[str]
    condition: Optional[str]
    color: Optional[str]
    size: Optional[str]
    material: Optional[str]
    gtin: Optional[str]
    mpn: Optional[str]
    image_link: Optional[str]
    additional_image_links: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "link": self.link,
            "price": self.price,
            "sale_price": self.sale_price,
            "availability": self.availability,
            "condition": self.condition,
            "color": self.color,
            "size": self.size,
            "material": self.material,
            "gtin": self.gtin,
            "mpn": self.mpn,
            "image_link": self.image_link,
            "additional_image_links": self.additional_image_links,
        }


@dataclass
class Product:
    group_id: Optional[str]
    title: Optional[str]
    description: Optional[str]
    brand: Optional[str]
    product_type: Optional[str]
    google_product_category: Optional[str]
    variants: list = field(default_factory=list)
    # Populated only by page enrichment (scraper.py --enrich); see enrich.py.
    specifications: Optional[dict] = None
    full_description: Optional[str] = None

    def to_dict(self) -> dict:
        data = {
            "group_id": self.group_id,
            "title": self.title,
            "description": self.description,
            "brand": self.brand,
            "product_type": self.product_type,
            "google_product_category": self.google_product_category,
            "variant_count": len(self.variants),
        }
        # Only surface enrichment fields when present, so un-enriched output
        # keeps its original shape.
        if self.specifications is not None:
            data["specifications"] = self.specifications
        if self.full_description is not None:
            data["full_description"] = self.full_description
        data["variants"] = [v.to_dict() for v in self.variants]
        return data


# --- Fetching -----------------------------------------------------------------
def http_get(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: int = 60,
    retries: int = 3,
) -> bytes:
    """GET ``url`` with a descriptive User-Agent and retry-with-backoff.

    Shared by the feed fetch and the page enrichment so both use the same polite
    network behaviour. Returns the response body as bytes.
    """
    get = (session or requests).get
    headers = {"User-Agent": USER_AGENT}
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as err:  # network / HTTP error
            last_err = err
            if attempt == retries:
                raise
            backoff = 2 ** (attempt - 1)
            print(
                f"  fetch attempt {attempt} failed ({err}); retrying in {backoff}s",
                file=sys.stderr,
            )
            time.sleep(backoff)
    raise last_err  # pragma: no cover - loop always returns or raises


def fetch_feed(
    url: str,
    cache_path: Optional[str] = None,
    timeout: int = 60,
    retries: int = 3,
) -> tuple[bytes, float, bool]:
    """Return ``(feed_bytes, seconds, from_cache)``.

    If ``cache_path`` exists it is read from disk (polite, fast for dev). Network
    fetches use a descriptive User-Agent and retry with exponential backoff.
    """
    start = time.perf_counter()

    if cache_path:
        try:
            with open(cache_path, "rb") as fh:
                data = fh.read()
            return data, time.perf_counter() - start, True
        except FileNotFoundError:
            pass  # fall through to network fetch, then populate the cache

    data = http_get(url, timeout=timeout, retries=retries)

    if cache_path:
        with open(cache_path, "wb") as fh:
            fh.write(data)

    return data, time.perf_counter() - start, False


# --- Parsing helpers ----------------------------------------------------------
_PRICE_RE = re.compile(r"(?P<amount>[\d\s.,]+?)\s*(?P<currency>[A-Za-z]{3})?\s*$")


def parse_price(raw: Optional[str]) -> Optional[dict]:
    """Turn ``"3955.50 SEK"`` into ``{"amount": 3955.5, "currency": "SEK"}``.

    Handles both dot and comma decimals, thousands separators, and the
    trailing-zero noise seen in the feed (``"57900.0000000000 SEK"``). Returns
    ``None`` when no amount can be parsed.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = _PRICE_RE.match(raw)
    if not m:
        return None

    amount_str = m.group("amount").strip()
    currency = m.group("currency")

    # Normalise the number. If both separators appear, the last one is the
    # decimal separator and the other groups thousands.
    amount_str = amount_str.replace(" ", "")
    if "," in amount_str and "." in amount_str:
        if amount_str.rfind(",") > amount_str.rfind("."):
            amount_str = amount_str.replace(".", "").replace(",", ".")
        else:
            amount_str = amount_str.replace(",", "")
    elif "," in amount_str:
        # A lone comma is treated as the decimal separator.
        amount_str = amount_str.replace(",", ".")

    try:
        amount = float(amount_str)
    except ValueError:
        return None

    return {"amount": amount, "currency": currency}


# Size / colour / material are NOT structured fields in this feed, so we derive
# them conservatively from the title. Only confident matches are kept; anything
# ambiguous stays null rather than guessing (honest data beats fabricated data).
_SIZE_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2)\b", re.IGNORECASE)
_SIZE_DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)"
    r"(?:\s*[x×]\s*(\d+(?:[.,]\d+)?))?\s*(mm|cm|m)?\b",
    re.IGNORECASE,
)

# Swedish colour words, longest-first so "antracitgrå" wins over "grå".
_COLOR_WORDS = [
    "antracitgrå", "gråbeige", "gråbrun", "mörkgrå", "ljusgrå",
    "antracit", "grå", "svart", "vit", "grön", "röd", "brun",
    "blå", "beige", "natur", "silver", "koppar", "mässing",
    "creme", "gul", "rosa", "turkos",
]
_COLOR_RE = re.compile(r"\b(" + "|".join(_COLOR_WORDS) + r")\b", re.IGNORECASE)

_MATERIAL_WORDS = [
    "aluminium", "härdat glas", "säkerhetsglas", "glas", "polykarbonat",
    "akryl", "rostfritt", "rostfri", "trä", "furu", "ek", "teak",
    "rotting", "konstrotting", "textil", "plast", "stål", "järn", "betong",
]
_MATERIAL_RE = re.compile(r"\b(" + "|".join(_MATERIAL_WORDS) + r")\b", re.IGNORECASE)


def derive_size(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    m = _SIZE_AREA_RE.search(title)
    if m:
        return f"{m.group(1)} m²"
    m = _SIZE_DIM_RE.search(title)
    if m and (m.group(3) or m.group(4)):  # require a 3rd dim or an explicit unit
        parts = [p for p in (m.group(1), m.group(2), m.group(3)) if p]
        unit = m.group(4) or ""
        return "x".join(parts) + (f" {unit}" if unit else "")
    return None


def derive_color(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    m = _COLOR_RE.search(title)
    return m.group(1).lower() if m else None


def derive_material(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    m = _MATERIAL_RE.search(title)
    return m.group(1).lower() if m else None


def _text(entry, tag: str) -> Optional[str]:
    """Text of the first ``g:<tag>`` child of an entry, stripped, or None."""
    el = entry.find(g(tag))
    if el is None or el.text is None:
        return None
    text = el.text.strip()
    return text or None


def _texts(entry, tag: str) -> list:
    """All non-empty texts for a repeating ``g:<tag>`` child (e.g. images)."""
    out = []
    for el in entry.findall(g(tag)):
        if el.text and el.text.strip():
            out.append(el.text.strip())
    return out


def entry_to_variant(entry) -> Variant:
    title = _text(entry, "title")
    return Variant(
        id=_text(entry, "id"),
        title=title,
        link=_text(entry, "link"),
        price=parse_price(_text(entry, "price")),
        sale_price=parse_price(_text(entry, "sale_price")),
        availability=_text(entry, "availability"),
        condition=_text(entry, "condition"),
        color=_text(entry, "color") or derive_color(title),
        size=_text(entry, "size") or derive_size(title),
        material=_text(entry, "material") or derive_material(title),
        gtin=_text(entry, "gtin"),
        mpn=_text(entry, "mpn"),
        image_link=_text(entry, "image_link"),
        additional_image_links=_texts(entry, "additional_image_link"),
    )


def iter_entries(source) -> Iterator[etree._Element]:
    """Yield each ``<entry>`` element, clearing it afterwards to free memory.

    ``source`` may be a path or a file-like object of bytes.
    """
    context = etree.iterparse(source, events=("end",), tag=ENTRY_TAG, recover=True)
    for _event, entry in context:
        yield entry
        # Free the element and any preceding siblings we no longer need.
        entry.clear()
        parent = entry.getparent()
        if parent is not None:
            while entry.getprevious() is not None:
                del parent[0]


@dataclass
class ParsedEntry:
    """One feed ``<entry>`` split into its variant part and the product-level
    fields that need lifting during grouping."""

    variant: Variant
    group_id: Optional[str]
    description: Optional[str]
    brand: Optional[str]
    product_type: Optional[str]
    google_product_category: Optional[str]


def _lift(values: Iterable[Optional[str]]) -> Optional[str]:
    """Lift a shared attribute to the product level.

    Returns the first non-null value among the variants. (When variants
    disagree the first one wins; in this feed these fields are consistent within
    a group.)
    """
    for v in values:
        if v is not None:
            return v
    return None


def _product_title(titles: list) -> Optional[str]:
    """Best product-level title: the longest common prefix of the variant
    titles, trimmed to a word boundary, falling back to the first title.

    Trimming to a word boundary avoids cutting through a token — e.g. titles
    "… 12.5 m²" and "… 15 m²" share the raw prefix "… 1", but we want the title
    without the dangling "1".
    """
    titles = [t for t in titles if t]
    if not titles:
        return None
    if len(titles) == 1:
        return titles[0]

    prefix = titles[0]
    for t in titles[1:]:
        while not t.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return titles[0]

    # If the prefix ends mid-word (the next char in some title is alphanumeric),
    # drop the trailing partial token so we don't keep a dangling fragment.
    if any(len(t) > len(prefix) and t[len(prefix)].isalnum() for t in titles):
        prefix = prefix.rsplit(" ", 1)[0] if " " in prefix else ""

    prefix = prefix.strip(" -–—,|")
    return prefix or titles[0]


def group_products(entries: Iterable[ParsedEntry]) -> list:
    """Collapse entries sharing an ``item_group_id`` into products.

    Insertion order is preserved (dict is ordered) so output is deterministic.
    Entries with no group id each become their own single-variant product, keyed
    by their own id so the schema stays uniform.
    """
    groups: dict = {}
    for e in entries:
        key = e.group_id if e.group_id is not None else f"__solo__:{e.variant.id}"
        groups.setdefault(key, []).append(e)

    products = []
    for members in groups.values():
        products.append(
            Product(
                group_id=members[0].group_id,
                title=_product_title([m.variant.title for m in members]),
                description=_lift(m.description for m in members),
                brand=_lift(m.brand for m in members),
                product_type=_lift(m.product_type for m in members),
                google_product_category=_lift(
                    m.google_product_category for m in members
                ),
                variants=[m.variant for m in members],
            )
        )
    return products


def entry_to_parsed(entry) -> ParsedEntry:
    return ParsedEntry(
        variant=entry_to_variant(entry),
        group_id=_text(entry, "item_group_id"),
        description=_text(entry, "description"),
        brand=_text(entry, "brand"),
        product_type=_text(entry, "product_type"),
        google_product_category=_text(entry, "google_product_category"),
    )


def parse_feed(source, limit: Optional[int] = None) -> tuple[list, int]:
    """Parse ``source`` into a list of grouped products.

    Returns ``(products, entry_count)``. ``limit`` caps the number of *products*
    returned (post-grouping), for fast smoke tests.
    """
    entries = []
    entry_count = 0
    for entry in iter_entries(source):
        entry_count += 1
        entries.append(entry_to_parsed(entry))

    products = group_products(entries)
    if limit is not None:
        products = products[:limit]
    return products, entry_count


# --- Output -------------------------------------------------------------------
def build_document(products: list, source_url: str) -> dict:
    variant_count = sum(len(p.variants) for p in products)
    return {
        "source": source_url,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "product_count": len(products),
        "variant_count": variant_count,
        "products": [p.to_dict() for p in products],
    }


def write_json(document: dict, path: str, pretty: bool = True) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        if pretty:
            json.dump(document, fh, ensure_ascii=False, indent=2)
        else:
            json.dump(document, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")


# --- CLI ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape the Willab Garden Google Merchant feed into JSON.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--feed-url", default=DEFAULT_FEED_URL, help="Product feed URL.")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N products (post-grouping). For smoke tests.",
    )
    p.add_argument(
        "--raw-cache",
        default=None,
        metavar="PATH",
        help="Save the fetched feed here; reuse it on later runs if it exists.",
    )
    p.add_argument(
        "--enrich",
        action="store_true",
        help=(
            "Enrich each product from its page (colour, material, dimensions, "
            "full spec table, rich description). One request per product; slower "
            "but produces a richer JSON. See enrich.py."
        ),
    )
    p.add_argument(
        "--enrich-delay",
        type=float,
        default=0.3,
        metavar="SECONDS",
        help="Delay between enrichment page requests, to stay polite.",
    )
    pretty = p.add_mutually_exclusive_group()
    pretty.add_argument(
        "--pretty",
        dest="pretty",
        action="store_true",
        default=True,
        help="Pretty-print the JSON (default).",
    )
    pretty.add_argument(
        "--no-pretty",
        dest="pretty",
        action="store_false",
        help="Emit compact JSON.",
    )
    return p


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def main(argv: Optional[list] = None) -> int:
    # Ensure the summary (which contains "→", "m²", Swedish characters) prints
    # on consoles whose default encoding isn't UTF-8 (e.g. Windows cp1252).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    args = build_parser().parse_args(argv)

    data, fetch_secs, from_cache = fetch_feed(args.feed_url, cache_path=args.raw_cache)
    where = "cache" if from_cache else "feed"
    print(f"Fetched {where} ({human_size(len(data))}) in {fetch_secs:.1f}s")

    products, entry_count = parse_feed(io.BytesIO(data), limit=args.limit)
    variant_count = sum(len(p.variants) for p in products)
    grouped = variant_count - len(products)
    limit_note = f" (limited to {args.limit})" if args.limit is not None else ""
    print(
        f"Parsed {entry_count} items → {len(products)} products"
        f" ({grouped} variants grouped){limit_note}"
    )

    if args.enrich:
        # Imported lazily so the core scraper has no import-time coupling to the
        # enrichment path (and a failed enrich import can't break a plain run).
        from enrich import enrich_products

        print(f"Enriching {len(products)} products from their pages…")
        stats = enrich_products(products, delay=args.enrich_delay)
        print(
            f"Enriched {stats['enriched']}/{stats['attempted']} products"
            f" ({stats['failed']} failed)"
        )

    document = build_document(products, args.feed_url)
    write_json(document, args.output, pretty=args.pretty)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
