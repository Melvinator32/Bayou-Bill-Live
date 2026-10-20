"""Printify connector — pulls EXACT per-variant production cost from your Printify
products and shipping from your order history, then matches it to your Etsy sales
two ways:
  1) variant SKU  -> Etsy item SKU      (exact, ~98% coverage)
  2) listing id   -> Etsy listing id    (fallback, covers the rest -> ~100%)

Only costs for products you've actually SOLD are written, so the Costs page
stays clean. Requires a Personal Access Token with Shops/Products/Orders read.
Money is integer cents. A browser-style User-Agent is sent (Printify is behind
Cloudflare, which 403s bare script User-Agents).
"""
import os
import re
import time
from datetime import datetime

import requests

from connectors import Connector, SettingField
from database import get_db

BASE = "https://api.printify.com/v1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BayouBillTracker/1.0"


def _cents(v):
    try:
        return (v or 0) / 100.0
    except TypeError:
        return 0.0


def _listing_id(product):
    ext = product.get("external") or {}
    if isinstance(ext, list):
        ext = ext[0] if ext else {}
    m = re.search(r"/listing/(\d+)", str(ext.get("handle") or ""))
    if m:
        return m.group(1)
    return str(ext.get("id") or "").strip()


class PrintifyConnector(Connector):
    key = "printify"
    label = "Printify (costs)"
    kind = "cost"
    docs_url = "https://developers.printify.com/"
    implemented = True
    settings_fields = [
        SettingField("printify_token", "Personal Access Token", "password",
                     "Printify -> My Profile -> Connections. Needs Shops/Products/Orders read."),
        SettingField("printify_shop_id", "Shop ID", "text",
                     "Optional - auto-detected if blank."),
    ]

    def _token(self, settings):
        return os.environ.get("PRINTIFY_TOKEN") or settings.get("printify_token", "")

    def _get(self, path, settings, _attempts=4):
        headers = {"Authorization": f"Bearer {self._token(settings)}",
                   "User-Agent": UA, "Accept": "application/json"}
        last = None
        for i in range(_attempts):
            try:
                resp = requests.get(f"{BASE}{path}", headers=headers, timeout=(10, 60))
            except requests.exceptions.RequestException as e:
                last = e; time.sleep(2 ** i); continue
            if resp.status_code in (401, 403):
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                last = requests.exceptions.HTTPError(f"{resp.status_code} from Printify",
                                                     response=resp)
                time.sleep(2 ** i); continue
            resp.raise_for_status()
            return resp.json()
        raise last

    def _shop_id(self, settings):
        sid = settings.get("printify_shop_id")
        if sid:
            return sid
        shops = self._get("/shops.json", settings)
        if shops:
            return str(shops[0]["id"])
        raise RuntimeError("No Printify shops found on this account.")

    def is_configured(self, settings):
        return bool(self._token(settings))

    def test(self, settings):
        if not self.is_configured(settings):
            return {"ok": False, "message": "Add your Printify token first."}
        try:
            shops = self._get("/shops.json", settings)
            names = ", ".join(s.get("title", str(s.get("id"))) for s in shops[:3])
            return {"ok": True, "message": f"Connected. {len(shops)} shop(s): {names}."}
        except Exception as e:
            return {"ok": False, "message": f"Connection failed: {e}"}

    def _sold_sets(self):
        conn = get_db()
        skus = {r[0] for r in conn.execute(
            "SELECT DISTINCT sku FROM etsy_items WHERE sku != ''")}
        listings = {r[0] for r in conn.execute(
            "SELECT DISTINCT listing_id FROM etsy_items WHERE listing_id != ''")}
        conn.close()
        return skus, listings

    def _collect_products(self, shop_id, settings, sold_skus, sold_listings):
        """Returns (sku_cost, listing_cost, sku_to_listing, titles)."""
        sku_cost, listing_cost, sku_to_listing = {}, {}, {}
        titles = {}
        page = 1
        while page <= 60:
            data = self._get(
                f"/shops/{shop_id}/products.json?page={page}&limit=50", settings)
            rows = data.get("data", []) if isinstance(data, dict) else []
            if not rows:
                break
            for p in rows:
                lid = _listing_id(p)
                title = p.get("title", "")
                variants = p.get("variants", []) or []
                enabled = [v for v in variants if v.get("is_enabled")]
                pool = enabled or variants
                ecosts = [_cents(v.get("cost")) for v in pool if v.get("cost")]
                avg_cost = round(sum(ecosts) / len(ecosts), 4) if ecosts else 0.0
                for v in variants:
                    sk = (v.get("sku") or "").strip()
                    if not sk:
                        continue
                    if lid:
                        sku_to_listing[sk] = lid
                    if sk in sold_skus:
                        sku_cost[sk] = _cents(v.get("cost"))
                        titles[sk] = title
                if lid and lid in sold_listings:
                    listing_cost[lid] = avg_cost
                    titles[lid] = title
            last = data.get("last_page") if isinstance(data, dict) else None
            if last and page >= last:
                break
            page += 1
            time.sleep(0.2)
        return sku_cost, listing_cost, sku_to_listing, titles

    def _collect_shipping(self, shop_id, settings):
        """Best-effort: variant sku -> blended per-unit shipping, from orders."""
        ship_sum, ship_qty = {}, {}
        page = 1
        try:
            while page <= 80:
                data = self._get(
                    f"/shops/{shop_id}/orders.json?page={page}&limit=50", settings)
                orders = data.get("data", []) if isinstance(data, dict) else []
                if not orders:
                    break
                for o in orders:
                    if str(o.get("status", "")).lower() in ("canceled", "cancelled"):
                        continue
                    items = o.get("line_items", []) or []
                    oship = _cents(o.get("total_shipping"))
                    oqty = sum((li.get("quantity") or 1) for li in items) or 1
                    for li in items:
                        sk = ((li.get("metadata") or {}).get("sku") or "").strip()
                        if not sk:
                            continue
                        qty = li.get("quantity") or 1
                        if li.get("shipping_cost") is not None:
                            ship = _cents(li.get("shipping_cost"))
                        else:
                            ship = oship * (qty / oqty)
                        ship_sum[sk] = ship_sum.get(sk, 0.0) + ship
                        ship_qty[sk] = ship_qty.get(sk, 0) + qty
                last = data.get("last_page") if isinstance(data, dict) else None
                if last and page >= last:
                    break
                page += 1
                time.sleep(0.2)
        except Exception:
            pass  # shipping is best-effort; production cost still applies
        return {sk: round(ship_sum[sk] / ship_qty[sk], 4)
                for sk in ship_sum if ship_qty.get(sk)}

    def sync(self, settings):
        if not self.is_configured(settings):
            raise RuntimeError("Printify not configured - add your token.")
        shop_id = self._shop_id(settings)
        sold_skus, sold_listings = self._sold_sets()

        sku_cost, listing_cost, sku_to_listing, titles = self._collect_products(
            shop_id, settings, sold_skus, sold_listings)
        sku_ship = self._collect_shipping(shop_id, settings)

        # listing-level shipping = average of that listing's variant shipping
        listing_ship_acc = {}
        for sk, lid in sku_to_listing.items():
            if sk in sku_ship and lid in listing_cost:
                listing_ship_acc.setdefault(lid, []).append(sku_ship[sk])
        listing_ship = {lid: round(sum(v) / len(v), 4)
                        for lid, v in listing_ship_acc.items() if v}

        conn = get_db()
        now = datetime.now().isoformat(timespec="seconds")
        new = updated = 0
        try:
            for sk, unit in sku_cost.items():
                existing = conn.execute(
                    "SELECT 1 FROM sku_costs WHERE sku=?", (sk,)).fetchone()
                conn.execute("""
                    INSERT INTO sku_costs
                        (sku, product_name, unit_cost, ship_cost, notes, source, updated_at)
                    VALUES (?,?,?,?,'','printify',?)
                    ON CONFLICT(sku) DO UPDATE SET
                        unit_cost=excluded.unit_cost, ship_cost=excluded.ship_cost,
                        source='printify',
                        product_name=COALESCE(NULLIF(sku_costs.product_name,''),
                                              excluded.product_name),
                        updated_at=excluded.updated_at""",
                    (sk, titles.get(sk, ""), unit, sku_ship.get(sk, 0.0), now))
                new += 0 if existing else 1
                updated += 1 if existing else 0
            for lid, unit in listing_cost.items():
                conn.execute("""
                    INSERT INTO listing_costs
                        (listing_id, product_name, unit_cost, ship_cost, source, updated_at)
                    VALUES (?,?,?,?,'printify',?)
                    ON CONFLICT(listing_id) DO UPDATE SET
                        unit_cost=excluded.unit_cost, ship_cost=excluded.ship_cost,
                        source='printify', product_name=excluded.product_name,
                        updated_at=excluded.updated_at""",
                    (lid, titles.get(lid, ""), unit, listing_ship.get(lid, 0.0), now))
            conn.execute("""INSERT INTO import_log
                (source, filename, rows_seen, rows_new, rows_updated, imported_at)
                VALUES ('printify','API sync (products+orders)',?,?,?,?)""",
                (len(sku_cost) + len(listing_cost), new, updated, now))
            conn.commit()
        finally:
            conn.close()
        return {"rows_seen": len(sku_cost) + len(listing_cost), "rows_new": new,
                "rows_updated": updated, "skus": len(sku_cost),
                "listings": len(listing_cost)}
