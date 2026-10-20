"""Read-only Printify probe. Sends a normal User-Agent so Printify's Cloudflare
front-end doesn't block it (that was the earlier 403). Stdlib only; changes nothing.

Run from the app folder:
    python printify_probe.py
"""
import json
import re
import sqlite3
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
    handle = str(ext.get("handle") or "")
    m = re.search(r"/listing/(\d+)", handle)
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
        print("No Printify token saved. Paste it on the Connectors page first.")
        return

    code, body = api_get("/shops.json", token)
    print(f"/shops.json -> HTTP {code}")
    if code == 200 and isinstance(body, list) and body:
        shop = str(body[0]["id"])
        print(f"  shop id: {shop}")
    elif not shop:
        print("  Could not read shops and no shop id stored. Stopping.")
        return

    etsy_listing_ids = {str(r[0]) for r in
                        c.execute("SELECT DISTINCT listing_id FROM etsy_items "
                                  "WHERE listing_id != ''")}
    print(f"\nEtsy sales reference {len(etsy_listing_ids)} distinct listing ids.")

    pcode, prods = api_get(f"/shops/{shop}/products.json?limit=20", token)
    print(f"/products.json -> HTTP {pcode}\n")
    if pcode != 200:
        print("Products still blocked. Paste this output and we'll switch tactics.")
        return
    data = prods.get("data", []) if isinstance(prods, dict) else []

    print(f"=========== FIRST {min(len(data),5)} PRODUCTS ===========")
    for p in data[:5]:
        lid = listing_id_from(p)
        print(f"\nproduct_id : {p.get('id')}")
        print(f"title      : {(p.get('title') or '')[:50]}")
        print(f"external   : {json.dumps(p.get('external'))[:200]}")
        print(f"-> listing id: {lid!r}   matches an Etsy sale? {lid in etsy_listing_ids}")
        for v in (p.get('variants') or [])[:2]:
            print(f"   variant sku={v.get('sku')!r} cost={v.get('cost')} "
                  f"enabled={v.get('is_enabled')}")

    allmatch = sum(1 for p in data if listing_id_from(p) in etsy_listing_ids)
    print(f"\nOf {len(data)} products, {allmatch} extracted a listing id that "
          f"matches an Etsy sale.")
    c.close()
    print("\nCopy this whole output back to the chat.\n")


if __name__ == "__main__":
    main()
