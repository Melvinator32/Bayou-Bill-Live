"""Read-only: shows how Printify labels your orders (order_type), so auto-pull can
separate YOUR personal orders from customer/Etsy-fulfillment orders. Stdlib only,
proper User-Agent, changes nothing.

Run from the app folder:
    python printify_ordertypes.py
"""
import json
import sqlite3
import time
import urllib.request
import urllib.error
from collections import Counter

BASE = "https://api.printify.com/v1"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BayouBillTracker/1.0",
     "Accept": "application/json"}


def api_get(path, token):
    h = dict(H); h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


def main():
    c = sqlite3.connect("tracker.db"); c.row_factory = sqlite3.Row
    s = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    token = s.get("printify_token"); shop = s.get("printify_shop_id")
    if not token:
        print("No token saved."); return
    if not shop:
        code, body = api_get("/shops.json", token)
        if code == 200 and body:
            shop = str(body[0]["id"])
        else:
            print(f"/shops.json HTTP {code}; put Shop ID on Connectors page."); return

    etsy_orders = {str(r[0]) for r in c.execute("SELECT order_id FROM etsy_orders")}

    types = Counter()
    samples = {}        # order_type -> list of sample dicts
    ext_match = 0; ext_total = 0
    page = 1; fetched = 0
    while page <= 6:
        code, data = api_get(f"/shops/{shop}/orders.json?page={page}&limit=50", token)
        if code != 200:
            print(f"orders page {page} -> HTTP {code}; stopping."); break
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not rows:
            break
        for o in rows:
            fetched += 1
            md = o.get("metadata") or {}
            ot = str(md.get("order_type") or "(none)")
            types[ot] += 1
            addr = o.get("address_to") or {}
            name = f"{(addr.get('first_name') or '')[:1]}. {addr.get('last_name') or ''}".strip()
            if ot != "external":
                samples.setdefault(ot, [])
                if len(samples[ot]) < 3:
                    samples[ot].append({
                        "id": o.get("id"), "status": o.get("status"),
                        "total_price": o.get("total_price"),
                        "total_shipping": o.get("total_shipping"),
                        "label": md.get("shop_order_label"), "to": name})
            else:
                ext_total += 1
                sid = str(md.get("shop_order_id") or md.get("shop_order_label") or "")
                if sid in etsy_orders:
                    ext_match += 1
        last = data.get("last_page") if isinstance(data, dict) else None
        if last and page >= last:
            break
        page += 1; time.sleep(0.2)

    print(f"\nFetched {fetched} Printify orders.\n")
    print("order_type distribution:")
    for k, v in types.most_common():
        print(f"   {k:<12} {v}")
    print(f"\n'external' orders whose id matches an Etsy order in your db: "
          f"{ext_match}/{ext_total} (confirms external = customer/Etsy orders)")
    print("\nNON-external samples (these are the candidates for personal purchases):")
    if not samples:
        print("   (none found in the fetched pages)")
    for ot, lst in samples.items():
        print(f"\n  order_type = {ot}:")
        for x in lst:
            print(f"    id={x['id']} status={x['status']} "
                  f"total_price={x['total_price']} ship={x['total_shipping']} "
                  f"label={x['label']!r} to={x['to']!r}")
    c.close()
    print("\nCopy this whole output back to the chat.\n")


if __name__ == "__main__":
    main()
