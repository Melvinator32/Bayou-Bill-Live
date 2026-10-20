"""Read-only probe of the Printify ORDERS endpoint only (the one your token can
already read) + your tracker.db. Confirms the Etsy order-id bridge. Stdlib only;
changes nothing.

Run from the app folder:
    python printify_orders_probe.py
"""
import json
import sqlite3
import urllib.request
import urllib.error

BASE = "https://api.printify.com/v1"


def api_get(path, token):
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    c = sqlite3.connect("tracker.db")
    c.row_factory = sqlite3.Row
    s = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")}
    token = s.get("printify_token")
    shop = s.get("printify_shop_id")
    if not token:
        print("No Printify token saved. Open the app and save it first.")
        return
    if not shop:
        try:
            shop = str(api_get("/shops.json", token)[0]["id"])
        except Exception:
            print("No shop id stored and couldn't auto-detect. Put your Shop ID in")
            print("the Connectors page (it's the number in your Printify store URL).")
            return

    etsy_order_ids = {str(r[0]) for r in
                      c.execute("SELECT order_id FROM etsy_orders")}
    print(f"\ntracker.db has {len(etsy_order_ids)} Etsy order ids.\n")

    try:
        orders = api_get(f"/shops/{shop}/orders.json?limit=5", token)
    except urllib.error.HTTPError as e:
        print(f"Orders call failed: HTTP {e.code} {e.reason}")
        return
    data = orders.get("data", []) if isinstance(orders, dict) else []
    print(f"=========== FIRST {min(len(data),3)} PRINTIFY ORDERS ===========")
    for o in data[:3]:
        md = o.get("metadata") or {}
        print(f"\nprintify order id : {o.get('id')}")
        print(f"status            : {o.get('status')}")
        print(f"metadata          : {json.dumps(md)[:300]}")
        print(f"total_price={o.get('total_price')} total_shipping={o.get('total_shipping')}")
        # which metadata values look like one of our Etsy order ids?
        for k, v in md.items():
            if str(v) in etsy_order_ids:
                print(f"   >>> metadata['{k}'] = {v}  MATCHES an Etsy order id")
        for li in (o.get("line_items") or [])[:2]:
            lmd = li.get("metadata") or {}
            print(f"   line: product_id={li.get('product_id')} qty={li.get('quantity')} "
                  f"cost={li.get('cost')} ship={li.get('shipping_cost')} "
                  f"sku={lmd.get('sku')!r}")

    # broad test: across these orders, how many have a metadata field matching Etsy
    hits = 0
    for o in data:
        md = o.get("metadata") or {}
        if any(str(v) in etsy_order_ids for v in md.values()):
            hits += 1
    print(f"\nOf {len(data)} Printify orders, {hits} had a metadata id matching an "
          f"Etsy order id in your db.")
    c.close()
    print("\nCopy this whole output back to the chat.\n")


if __name__ == "__main__":
    main()
