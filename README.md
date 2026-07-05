# Willab Garden product scraper

A small, single-file Python scraper that collects the **entire Willab Garden
catalogue** from the retailer's public Google Merchant product feed, groups
product variants correctly, and writes a structured JSON file.

```bash
python scraper.py --limit 10     # fast smoke test (seconds)
python scraper.py                # full catalogue
```

On the live feed this produces **792 products from 13,649 variants**.

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
| `--pretty` / `--no-pretty` | pretty | Pretty-print (default) or emit compact JSON. |

---

## Output

The scraper writes a single JSON document. Swedish characters (å ä ö) are
preserved (`ensure_ascii=False`), prices are structured into an amount and
currency, and every product carries a uniform `variants` array — even
standalone products (a single-element array), so consumers never special-case.

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
      "variants": [
        {
          "id": "3024SR",
          "title": "Green Room Classic växthus 24.4 m²",
          "link": "https://www.willabgarden.se/…/green-room-classic-vaxthus-3024sr/",
          "price": { "amount": 123900.0, "currency": "SEK" },
          "sale_price": null,
          "availability": "in stock",
          "condition": "new",
          "color": null,
          "size": "24.4 m²",
          "material": null,
          "gtin": null,
          "mpn": null,
          "image_link": "https://…",
          "additional_image_links": ["https://…", "https://…"]
        }
      ]
    }
  ]
}
```

A committed [`sample_output.json`](sample_output.json) (generated with
`--limit 10`) lets you see the exact shape without running anything.

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

The scraper therefore **derives these attributes conservatively from the
title** when they are unambiguously present, and leaves them `null` otherwise:

- **`size`** — an area (`24.4 m²`) or an explicit dimension is extracted from
  the title (populated for ~20% of variants, mostly greenhouses).
- **`material`** — set when a known material word (aluminium, glas,
  polykarbonat, trä, …) appears in the title.
- **`color`** — set only when a colour word appears in the title. Colour is
  almost never in the title here, so this is usually `null`. Guessing colour
  from ambiguous SKU letters was deliberately avoided: a wrong colour is worse
  than an honest `null`.

If a future feed adds the structured `g:color`/`g:size`/`g:material` fields, the
parser prefers those and falls back to derivation only when they are missing.

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

---

## Tests

An optional schema test validates the output structure:

```bash
pip install pytest
python scraper.py --limit 10          # produce willabgarden_products.json
pytest
```

It checks that every product has a non-empty `variants` list and that every
variant has an `id` and a parsed `price`. It also exercises the price parser and
the title-derivation helpers directly.

---

## Possible extensions

The feed reflects what the retailer exposes to Google Merchant and may omit some
page-level detail (e.g. the "Bra att veta" care notes) or lag the live site. A
future version could enrich selected products via the site's Hello Retail search
API (`core.helloretail.com`, the `websiteUuid` is present in the page) or
targeted page scraping — layered on top of this feed parser, which stays the
core of the solution.

---

## License

[MIT](LICENSE).
