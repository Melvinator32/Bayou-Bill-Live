"""Measures, across your WHOLE Printify catalog, how well two bridges cover your
Etsy sales: (A) variant SKU -> Etsy item SKU, and (B) product listing id -> Etsy
listing id. Read-only, stdlib only, proper User-Agent. Changes nothing.

Run from the app folder:
    python printify_coverage.py
"""
import json
import re
import sqlite3
import time
import urllib.request
import urllib.error

BASE = "https://api.printify.com/v1"
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BayouBillTracker/1.0",
    "Accept": "application/json",
}


def api_get(path, token):
    h = dict(HEADERS_BASE)
    h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


def listing_id_from(product):
    ext = product.get("external") or {}
    if isinstance(ext, list):
        ext = ext[0] if ext else {}
    m = re.search(r"/listing/(\d+)", str(ext.get("handle") or ""))
    if m:
        return m.group(1)
    return str(ext.get("id") or "").strip()


def main():
    c = sqlite3.connect("tracker.db")
    c.row_factory = sqlite3.Row
    s = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    token = s.get("printify_token")
    shop = s.get("printify_shop_id")
    if not token:
        print("No token saved."); return
    if not shop:
        code, body = api_get("/shops.json", token)
        if code == 200 and body:
            shop = str(body[0]["id"])

    etsy_skus = {str(r[0]) for r in c.execute(
        "SELECT DISTINCT sku FROM etsy_items WHERE sku != ''")}
    etsy_listings = {str(r[0]) for r in c.execute(
        "SELECT DISTINCT listing_id FROM etsy_items WHERE listing_id != ''")}

    prod_variant_skus = set()
    prod_listing_ids = set()
    n_products = 0
    page = 1
    while page <= 30:
        code, data = api_get(f"/shops/{shop}/products.json?page={page}&limit=50", token)
        if code != 200:
            print(f"products page {page} -> HTTP {code}; stopping.")
            break
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not rows:
            break
        for p in rows:
            n_products += 1
            lid = listing_id_from(p)
            if lid:
                prod_listing_ids.add(lid)
            for v in (p.get("variants") or []):
                sk = (v.get("sku") or "").strip()
                if sk:
                    prod_variant_skus.add(sk)
        last = data.get("last_page") if isinstance(data, dict) else None
        if last and page >= last:
            break
        page += 1
        time.sleep(0.2)

    sku_hits = len(etsy_skus & prod_variant_skus)
    lst_hits = len(etsy_listings & prod_listing_ids)

    print("\n==================== COVERAGE ====================")
    print(f"Printify products scanned: {n_products}")
    print(f"Printify variant SKUs:     {len(prod_variant_skus)}")
    print(f"Printify listing ids:      {len(prod_listing_ids)}")
    print()
    print(f"Distinct Etsy SKUs in sales:      {len(etsy_skus)}")
    print(f"   matched by variant SKU:        {sku_hits}  "
          f"({round(100*sku_hits/max(len(etsy_skus),1))}%)")
    print(f"Distinct Etsy listing ids in sales: {len(etsy_listings)}")
    print(f"   matched by product listing id:   {lst_hits}  "
          f"({round(100*lst_hits/max(len(etsy_listings),1))}%)")
    print("\n  --- 3 sample Printify variant SKUs ---")
    for x in list(prod_variant_skus)[:3]:
        print("   ", x)
    print("  --- 3 sample Etsy SKUs ---")
    for x in list(etsy_skus)[:3]:
        print("   ", x)
    c.close()
    print("\nCopy this whole output back to the chat.\n")


if __name__ == "__main__":
    main()
