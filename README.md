# Willab Garden product scraper

A small Python scraper that collects the **entire Willab Garden catalogue** from
the retailer's public Google Merchant product feed, groups product variants
correctly, and writes a structured JSON file.

```bash
python scraper.py --limit 10     # fast smoke test (seconds)
python scraper.py                # full catalogue
python scraper.py --enrich       # full catalogue + per-page enrichment (richer)
```

On the live feed this produces **792 products from 13,649 variants**.

The optional [`--enrich`](#optional-enrichment---enrich) step visits each product
page to recover attributes the feed omits (colour, material, dimensions, the full
specification table, and a rich description) — raising colour coverage from ~0%
to ~70% and adding a complete spec table to every product. The feed remains the
reliable backbone; enrichment only fills gaps and adds detail.

---

## Approach & rationale

**The scraper reads the retailer's Google Merchant product feed, not the
rendered website.** This is the central design decision.

Willab Garden's site is client-side rendered: a plain HTTP `GET` of a category
or product page returns an essentially empty `<main>` (only meta tags), so
scraping the HTML would require driving a headless browser and would break on
any front-end change. Instead, the site publishes a Google Merchant feed at:

```
https://www.willabgarden.se/googleproductfeed
```

Building on that feed is:

- **Complete** — the whole catalogue arrives in a single request.
- **Robust** — it follows the fixed Google Merchant field schema, which is
  stable across site redesigns.
- **Variant-aware** — variants are natively grouped by `item_group_id`, so we
  don't have to reverse-engineer relationships from the HTML.
- **Legitimate** — it is data the retailer publishes explicitly to be consumed
  by machines. No auth, no rate-limit games.

### What the real feed actually looks like

The feed was inspected before writing the parser, and a couple of things differ
from the "standard" Google Merchant/RSS assumption — the parser is built for the
feed as it really is:

- It is an **Atom 1.0** document (`<feed>` / `<entry>`), **not** RSS 2.0
  (`<channel>` / `<item>`). Product fields still use the Google Merchant
  namespace `http://base.google.com/ns/1.0` (the `g:` prefix).
- The following fields **are** present and reliable: `g:id`, `g:title`,
  `g:description`, `g:link`, `g:image_link`, `g:additional_image_link`
  (repeating), `g:price`, `g:sale_price` (~40% of items), `g:availability`,
  `g:condition`, `g:brand` (~19%), `g:product_type`, and `g:item_group_id`
  (present on **100%** of items).
- The following fields the brief anticipated are **absent** from this feed:
  `g:color`, `g:size`, `g:material`, `g:gtin`, `g:mpn`, and
  `g:google_product_category`. They are kept in the output schema (set to `null`
  when unavailable) so the shape stays predictable — see
  [Variant attributes](#variant-attributes-color--size--material).

The feed is ~55 MB, so it is parsed with a **streaming** `lxml.iterparse`
loop that clears each `<entry>` after use rather than loading the whole tree
into memory.

---

## Setup

Requires **Python 3.11+**.

```bash
python -m venv .venv && source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## Usage

```bash
# Fast verification: only the first 10 products (post-grouping).
python scraper.py --limit 10

# Full catalogue → willabgarden_products.json
python scraper.py

# Develop politely: cache the feed on first run, reuse it afterwards.
python scraper.py --raw-cache raw_feed.xml
```

Every run prints a summary:

```
Fetched feed (54.0 MB) in 7.4s
Parsed 13649 items → 792 products (12857 variants grouped)
Wrote willabgarden_products.json
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--feed-url URL` | the Willab Garden feed | Source feed URL. |
| `--output PATH` | `willabgarden_products.json` | Output JSON path. |
| `--limit N` | *(all)* | Process only the first N **products** (post-grouping). Great for a fast smoke test. |
| `--raw-cache PATH` | *(off)* | Save the fetched feed to this path; if it already exists, parse it instead of re-fetching. Faster iteration, polite to the server. |
| `--enrich` | *(off)* | Visit each **variant's** page to add colour, material, dimensions, the full spec table, and a rich description. See [below](#optional-enrichment---enrich). |
| `--enrich-workers N` | `8` | Concurrent workers for enrichment page fetches. |
| `--enrich-delay SECONDS` | `0.1` | Minimum delay between enrichment requests, to stay polite. |
| `--enrich-cache DIR` | *(off)* | Cache fetched product pages here; reused on later runs so re-enriching is near-instant. |
| `--pretty` / `--no-pretty` | pretty | Pretty-print (default) or emit compact JSON. |

---

## Optional enrichment (`--enrich`)

The feed is complete and reliable but omits structured attributes such as
**colour, material, dimensions, and the full specification table**. That data is
not in the server-rendered HTML (the page is client-side rendered and ships an
empty `<main>`), but it *is* embedded in a JSON hydration blob under
`pageContent.product` in each page. [`enrich.py`](enrich.py) extracts that blob
**without a headless browser** — a plain `requests` GET plus JSON parsing — and
merges the result onto the feed's variants.

```bash
# Full catalogue, enriched, with a page cache so re-runs are near-instant:
python scraper.py --enrich --enrich-cache .page_cache

# Quick enriched sample:
python scraper.py --limit 5 --enrich --enrich-cache .page_cache
```

What it adds:

- Fills `color` / `material` / `size` on variants where the feed left them
  `null` (from the variant's "Produktspecifikation" table).
- Adds a per-variant **`specifications`** object — the full spec table as
  key/value pairs (e.g. `Färg`, `Material väggar`, `Stormgaranti`, `Typ av glas`).
- Adds a product-level **`full_description`** — the page's rich description text.

### Why enrichment is per **variant**, not per product

This is the key correctness point. Colour, dimensions and the spec table **differ
between the variants of one product** — a greenhouse comes as a matrix of sizes ×
colours, and *each combination is its own SKU with its own page*. A single page
only describes its own SKU; it can't tell you a sibling's colour. So enrichment
fetches **each variant's own page** and applies that page's attributes to *that
variant only*.

(An earlier version fetched one page per product group and copied its colour onto
every variant — which was wrong for every multi-colour product. Fetching per
variant is the fix; see the report for the full story.)

Because that is up to ~13.6k page fetches on the full catalogue, enrichment is:

- **concurrent** — a bounded thread pool (`--enrich-workers`, default 8),
- **cached** — pages are stored on disk (`--enrich-cache DIR`), so re-runs are
  near-instant and don't re-hit the server,
- **polite** — a minimum delay between requests (`--enrich-delay`), and
- **opt-in** — only runs with `--enrich`.

Measured on a 413-variant sample spanning all six categories, with each variant
enriched from its own page:

| Attribute | Feed only | With `--enrich` |
| --- | --- | --- |
| `color` | ~0% | **68%** |
| `size` | 20% | **38%** |
| `material` | 4% | **31%** |
| `specifications` | — | **100%** of variants |
| `full_description` | — | **100%** of products |

Other design notes:

- **Feed data wins.** Enrichment only fills `null`s and adds new fields; it never
  overwrites a value already present from the feed.
- **Best-effort and safe** — the `pageContent.product` shape is an internal CMS
  structure that could change. A page that can't be fetched or parsed simply
  leaves that variant's feed data untouched; enrichment never aborts the run.
- **Output shape is stable** — the per-variant `specifications` and product-level
  `full_description` keys only appear when enrichment runs, so plain (un-enriched)
  output is unchanged.

---

## Output

The scraper writes a single JSON document. Swedish characters (å ä ö) are
preserved (`ensure_ascii=False`), prices are structured into an amount and
currency, and every product carries a uniform `variants` array — even
standalone products (a single-element array), so consumers never special-case.

The example below is from an **enriched** run. `full_description` is
product-level; the per-variant `specifications` and most `color`/`material`/`size`
values come from enrichment and are absent in a plain run. Note the two variants
carry **different colours** — each was enriched from its own page.

```json
{
  "source": "https://www.willabgarden.se/googleproductfeed",
  "scraped_at": "2026-07-05T12:00:00Z",
  "product_count": 792,
  "variant_count": 13649,
  "products": [
    {
      "group_id": "3249",
      "title": "Green Room Classic växthus",
      "description": "…",
      "brand": "Green Room",
      "product_type": "Växthus > Växthusmodeller > Stormsäkra växthus",
      "google_product_category": null,
      "variant_count": 73,
      "full_description": "Green Room är Willab Gardens serie för stormsäkra växthus…",
      "variants": [
        {
          "id": "3024SR",
          "title": "Green Room Classic växthus 24.4 m²",
          "link": "https://www.willabgarden.se/…/green-room-classic-vaxthus-3024sr/",
          "price": { "amount": 123900.0, "currency": "SEK" },
          "sale_price": null,
          "availability": "in stock",
          "condition": "new",
          "color": "RAL 3005 - Vinröd",
          "size": "24.4 m²",
          "material": "4 mm säkerhetsglas",
          "gtin": null,
          "mpn": null,
          "image_link": "https://…",
          "additional_image_links": ["https://…"],
          "specifications": {
            "Yta": "24,4 m²",
            "Färg": "RAL 3005 - Vinröd",
            "Material väggar": "4 mm säkerhetsglas",
            "Stormgaranti": "5 år"
          }
        },
        {
          "id": "3024SW",
          "title": "Green Room Classic växthus 24.4 m²",
          "color": "RAL 9010 - Vit",
          "size": "24.4 m²",
          "specifications": { "Färg": "RAL 9010 - Vit", "…": "…" }
        }
      ]
    }
  ]
}
```

A committed [`sample_output.json`](sample_output.json) — a small, enriched,
multi-category sample — lets you see the exact shape (including the per-variant
colours) without running anything.

### How variants are grouped

Every item in the feed carries a `g:item_group_id`. Items that share one are the
same product in different variants (e.g. a greenhouse in several sizes and frame
colours), so they are **collapsed into a single product object** with a
`variants` array. Shared attributes — the base title (computed as the longest
common prefix of the variants' titles), brand, product type, and description —
are **lifted to the product level**, while variant-specific data (id, price,
sale price, availability, images, link, and the derived attributes below) stays
on each variant. Items without a group id become standalone products wrapped in
a single-element `variants` array, keeping the schema uniform.

### Variant attributes (color / size / material)

This feed does **not** expose structured `g:color`, `g:size`, or `g:material`
fields — the only place that information appears is inside the free-text title
(e.g. "… 24.4 m²") or, for colour, encoded in the SKU suffix in an
inconsistent, undocumented way.

Without enrichment, the scraper **derives these attributes conservatively from
the title** when they are unambiguously present, and leaves them `null`
otherwise:

- **`size`** — an area (`24.4 m²`) or an explicit dimension is extracted from
  the title (populated for ~20% of variants, mostly greenhouses).
- **`material`** — set when a known material word (aluminium, glas,
  polykarbonat, trä, …) appears in the title.
- **`color`** — set only when a colour word appears in the title. Colour is
  almost never in the title here, so this is usually `null`. Guessing colour
  from ambiguous SKU letters was deliberately avoided: a wrong colour is worse
  than an honest `null`.

The precedence for each field is: **structured feed field → page enrichment
(if `--enrich`) → title derivation → `null`.** So a plain run relies on
derivation, while `--enrich` fills most of these gaps with real values from the
product's specification table — see
[Optional enrichment](#optional-enrichment---enrich).

---

## Robustness

- **Namespace-aware** Atom + Google Merchant (`g:`) parsing.
- **Price parsing** turns `"3955.50 SEK"` into
  `{ "amount": 3955.5, "currency": "SEK" }`, handling comma/dot decimals,
  thousands separators, and the trailing-zero noise present in the feed
  (`"57900.0000000000 SEK"`).
- **UTF-8 throughout**, including console output on Windows.
- **Repeating images** (`additional_image_link`) are collected into a list.
- **Missing fields never crash** — they default to `null`.
- **Network**: descriptive User-Agent, a request timeout, and a few retries
  with exponential backoff.
- **Idempotent**: re-running produces the same output; `--raw-cache` avoids
  re-hitting the server during development.
- **Enrichment is best-effort**: a page that can't be fetched or parsed leaves
  the feed data untouched rather than aborting the run.

---

## Tests

```bash
pip install pytest
pytest
```

The suite runs fully offline. It covers:

- **Feed parsing** ([tests/test_schema.py](tests/test_schema.py)) — grouping
  invariants (uniform `variants` array; every variant has an `id` and a parsed
  `price`), shared-attribute lifting, plus the price parser and title-derivation
  helpers.
- **Enrichment** ([tests/test_enrich.py](tests/test_enrich.py)) — blob
  extraction, spec-table parsing (including the inconsistent `</br>` markup), and
  the gap-filling merge semantics, all against real product-page fixtures saved
  in `tests/fixtures/`.

---

## Possible extensions

- **Normalise enriched values.** The spec-table values are kept verbatim (e.g.
  colour as `"RAL 3005 - Vinröd"`, size sometimes as a compound dimension
  string). A normalisation pass could split these into structured sub-fields.
- **Incremental enrichment.** Combined with the page cache, only re-fetch
  variants whose feed entry changed since the last run.
- **Hello Retail API.** The site also loads Hello Retail (`helloretailcdn.com`,
  `websiteUuid` present in the page). Its search API could provide an alternative
  structured source or power availability/recommendation data.

---

## License

[MIT](LICENSE).
