"""Live clothing discovery with direct retailer links and Telegram artifacts."""
import asyncio
import csv
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import config
import state


_task: state.TaskRecord | None = None

# Retailers with public Shopify predictive-search product feeds. Each request is
# read-only and produces direct product URLs; unavailable stores fail independently.
PRODUCT_STORES = {
    "tentree": "https://www.tentree.com",
    "untuckit": "https://www.untuckit.com",
    "american giant": "https://www.american-giant.com",
    "outerknown": "https://www.outerknown.com",
    "universal standard": "https://www.universalstandard.com",
    "marine layer": "https://www.marinelayer.com",
    "mnml": "https://mnml.la",
}

# Stable retailer search pages provide useful fallbacks when a product feed has
# no match. They are labeled as search links, never as verified individual items.
SEARCH_STORES = {
    "uniqlo": "https://www.uniqlo.com/us/en/search?q={q}",
    "nordstrom": "https://www.nordstrom.com/sr?origin=keywordsearch&keyword={q}",
    "target": "https://www.target.com/s?searchTerm={q}",
    "h&m": "https://www2.hm.com/en_us/search-results.html?q={q}",
    "zara": "https://www.zara.com/us/en/search?searchTerm={q}",
    "macys": "https://www.macys.com/shop/featured/{slug}",
}


def configure(task=None) -> None:
    global _task
    _task = task


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JerryPocketAgent/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def _product_search(store: str, base: str, query: str, count: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "q": query,
        "resources[type]": "product",
        "resources[limit]": min(10, count),
    })
    endpoint = f"{base}/search/suggest.json?{params}"
    data = _get_json(endpoint)
    products = data.get("resources", {}).get("results", {}).get("products", [])
    out = []
    for product in products:
        raw_url = str(product.get("url") or "")
        url = urllib.parse.urljoin(base, raw_url)
        try:
            price = float(product.get("price"))
        except (TypeError, ValueError):
            price = None
        out.append({
            "title": product.get("title"),
            "retailer": store,
            "product_type": product.get("type"),
            "price": price,
            "currency": "USD",
            "available": product.get("available"),
            "url": url,
            "image": product.get("image"),
            "verified_product": True,
            "source_endpoint": endpoint,
            "search_text": " ".join(str(product.get(k) or "") for k in
                                      ("title", "type", "tags", "body")),
        })
    return out


def _tokens(query: str) -> list[str]:
    ignored = {"for", "with", "under", "mens", "men", "womens", "women", "clothes",
               "clothing", "outfit", "buy", "find", "some", "that", "and", "the"}
    return [x for x in re.findall(r"[a-z0-9]+", query.lower()) if len(x) >= 3 and x not in ignored]


def _search_sync(product_query: str, context_query: str, max_price: float | None, limit: int,
                 preferred: list[str]) -> dict:
    stores = PRODUCT_STORES
    if preferred:
        wanted = {x.strip().lower() for x in preferred}
        narrowed = {name: url for name, url in PRODUCT_STORES.items() if name in wanted}
        stores = narrowed or PRODUCT_STORES
    products = []
    failures = []
    with ThreadPoolExecutor(max_workers=min(7, len(stores))) as pool:
        futures = {pool.submit(_product_search, name, base, product_query, limit): name
                   for name, base in stores.items()}
        for future in as_completed(futures):
            try:
                products.extend(future.result())
            except Exception as exc:
                failures.append({"retailer": futures[future], "error": str(exc)})

    tokens = _tokens(context_query)
    context_lower = context_query.lower()
    desired_gender = ("women" if re.search(r"\b(women|womens|woman|female)\b", context_lower)
                      else "men" if re.search(r"\b(men|mens|man|male)\b", context_lower)
                      else "")
    ranked = []
    seen = set()
    for product in products:
        if product["url"] in seen:
            continue
        seen.add(product["url"])
        if max_price is not None and product["price"] is not None and product["price"] > max_price:
            continue
        hay = product.pop("search_text", "").lower()
        if desired_gender == "women" and re.search(r"\b(men|mens|male)\b", hay) \
                and not re.search(r"\b(women|womens|female)\b", hay):
            continue
        if desired_gender == "men" and re.search(r"\b(women|womens|female)\b", hay) \
                and not re.search(r"\b(men|mens|male)\b", hay):
            continue
        matches = sum(1 for token in tokens if token in hay)
        if desired_gender and re.search(
                r"\b" + (r"women|womens|female" if desired_gender == "women"
                           else r"men|mens|male") + r"\b", hay):
            matches += 2
        if tokens and matches == 0:
            continue
        product["relevance_matches"] = matches
        ranked.append(product)
    ranked.sort(key=lambda x: (-x["relevance_matches"],
                               x["price"] if x["price"] is not None else float("inf")))
    ranked = ranked[:limit]

    # Always include a small set of normal search-page fallbacks so broad style
    # requests are not constrained to the Shopify retailers above.
    fallback_names = preferred or ["uniqlo", "nordstrom", "target", "h&m"]
    search_links = []
    encoded = urllib.parse.quote_plus(context_query)
    slug = re.sub(r"[^a-z0-9]+", "-", context_query.lower()).strip("-")
    for name in fallback_names:
        key = name.strip().lower()
        template = SEARCH_STORES.get(key)
        if not template:
            continue
        search_links.append({
            "title": f"Search {name.title()} for {context_query}",
            "retailer": key,
            "url": template.format(q=encoded, slug=slug),
            "verified_product": False,
            "note": "Retailer search page; confirm current price, size, and stock on the site.",
        })
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "query": context_query,
        "price_limit_usd": max_price,
        "products": ranked,
        "retailer_search_links": search_links,
        "source_failures": failures,
        "freshness_note": "Prices and availability are live observations and may change.",
    }


def _write_artifacts(payload: dict) -> list[str]:
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"task_{_task.id}" if _task is not None else f"clothing_{int(time.time())}"
    json_path = config.ARTIFACT_DIR / f"{stem}_clothing_links.json"
    csv_path = config.ARTIFACT_DIR / f"{stem}_clothing_links.csv"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    rows = payload.get("products", []) + payload.get("retailer_search_links", [])
    fields = ["title", "retailer", "product_type", "price", "currency", "available",
              "verified_product", "url", "image", "note"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    if _task is not None:
        state.add_artifact(_task, json_path)
        state.add_artifact(_task, csv_path)
    return [str(json_path), str(csv_path)]


async def find_clothing_links(args: dict) -> str:
    try:
        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: describe the clothing, style, occasion, or item to find."
        details = [str(args.get(k) or "").strip() for k in
                   ("gender", "size", "color", "style", "occasion")]
        full_query = " ".join([query, *[x for x in details if x]])
        max_price_raw = args.get("max_price_usd")
        max_price = float(max_price_raw) if max_price_raw not in (None, "") else None
        limit = max(1, min(20, int(args.get("max_results") or 10)))
        preferred = args.get("preferred_stores") or []
        if isinstance(preferred, str):
            preferred = [x.strip() for x in preferred.split(",") if x.strip()]
        payload = await asyncio.to_thread(
            _search_sync, query, full_query, max_price, limit, list(preferred))
        payload["artifacts"] = _write_artifacts(payload)
        if _task is not None:
            for item in payload["products"] + payload["retailer_search_links"]:
                state.add_source(_task, item["url"])
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"Error: clothing search failed: {exc}"


TOOLS = {
    "find_clothing_links": {
        "schema": {"type": "function", "function": {
            "name": "find_clothing_links",
            "description": (
                "Find live clothing products and retailer search links from a natural-language "
                "style request. Returns direct links, observed prices/availability when verifiable, "
                "and CSV/JSON artifacts for Telegram. Research only; does not purchase."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "gender": {"type": "string"}, "size": {"type": "string"},
                "color": {"type": "string"}, "style": {"type": "string"},
                "occasion": {"type": "string"},
                "max_price_usd": {"type": "number"},
                "max_results": {"type": "integer", "description": "1 to 20"},
                "preferred_stores": {"type": "array", "items": {"type": "string"}},
            }, "required": ["query"]},
        }},
        "fn": find_clothing_links,
    },
}
