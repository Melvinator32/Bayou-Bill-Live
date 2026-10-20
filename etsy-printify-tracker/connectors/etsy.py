"""
Etsy connector — reads your shop's orders (receipts) via the Etsy Open API v3
and writes them into the same order tables the CSV importer uses (tagged
channel='etsy'), so the dashboard treats API-synced and CSV-imported orders
identically and reconciles them on transaction_id (no duplicates).

Setup: run get_etsy_token.py once from the app folder to fill in the four
credentials below automatically.

Fees: Etsy's receipt data has no full fee breakdown, so the Etsy channel keeps
using the Settings fee model (the analytics layer already estimates fees). This
sync only populates revenue, units, shipping, and tax.
"""
import time
from datetime import datetime, timezone

import requests

from connectors import Connector, SettingField
from database import get_db

TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"


def _money(m):
    """Etsy money object {amount, divisor} -> float dollars."""
    if not isinstance(m, dict):
        return 0.0
    div = m.get("divisor") or 100
    try:
        return round((m.get("amount") or 0) / div, 2)
    except ZeroDivisionError:
        return 0.0


class EtsyConnector(Connector):
    key = "etsy_api"
    label = "Etsy (API)"
    kind = "sales"
    docs_url = "https://developer.etsy.com/documentation/reference/#operation/getShopReceipts"
    implemented = True
    settings_fields = [
        SettingField("etsy_keystring", "API Keystring", "text"),
        SettingField("etsy_shared_secret", "Shared Secret", "password"),
        SettingField("etsy_refresh_token", "OAuth Refresh Token", "password",
                     "Run get_etsy_token.py once to fill these in automatically."),
        SettingField("etsy_shop_id", "Shop ID", "text"),
    ]

    # ---- internals -------------------------------------------------------
    def _headers(self, settings, access_token):
        # Post-Feb-2026 Etsy requires keystring:secret in the x-api-key header.
        return {
            "x-api-key": f"{settings.get('etsy_keystring','')}:{settings.get('etsy_shared_secret','')}",
            "Authorization": f"Bearer {access_token}",
        }

    def _access_token(self, settings):
        """Mint a fresh 1-hour access token from the stored refresh token."""
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": settings.get("etsy_keystring", ""),
            "refresh_token": settings.get("etsy_refresh_token", ""),
        }, timeout=30)
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _get(self, path, settings, access_token, _attempts=4):
        """GET with retry/backoff; auth errors fail fast."""
        last = None
        for i in range(_attempts):
            try:
                resp = requests.get(f"{API_BASE}{path}",
                                    headers=self._headers(settings, access_token),
                                    timeout=(10, 60))
            except requests.exceptions.RequestException as e:
                last = e
                time.sleep(2 ** i)
                continue
            if resp.status_code in (401, 403):
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                last = requests.exceptions.HTTPError(
                    f"{resp.status_code} from Etsy", response=resp)
                time.sleep(2 ** i)
                continue
            resp.raise_for_status()
            return resp.json()
        raise last

    @staticmethod
    def _iso_date(epoch):
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            return ""

    # ---- interface -------------------------------------------------------
    def is_configured(self, settings):
        return bool(settings.get("etsy_keystring")
                    and settings.get("etsy_shared_secret")
                    and settings.get("etsy_refresh_token")
                    and settings.get("etsy_shop_id"))

    def test(self, settings):
        if not self.is_configured(settings):
            return {"ok": False,
                    "message": "Missing credentials — run get_etsy_token.py first."}
        try:
            token = self._access_token(settings)
            me = self._get("/users/me", settings, token)
            return {"ok": True,
                    "message": f"Connected. Etsy user {me.get('user_id', '?')}, "
                               f"shop {settings.get('etsy_shop_id')}."}
        except Exception as e:
            return {"ok": False, "message": f"Connection failed: {e}"}

    def _collect(self, settings, token):
        """Network phase: page through receipts; return (orders, items)."""
        shop_id = settings.get("etsy_shop_id")
        orders, items = [], []
        offset, limit = 0, 100
        while True:
            data = self._get(
                f"/shops/{shop_id}/receipts?limit={limit}&offset={offset}",
                settings, token)
            results = data.get("results", []) if isinstance(data, dict) else []
            for r in results:
                oid = str(r.get("receipt_id"))
                sale_date = self._iso_date(
                    r.get("created_timestamp") or r.get("create_timestamp"))
                orders.append({
                    "order_id": oid,
                    "sale_date": sale_date,
                    "buyer": r.get("name", ""),
                    "order_shipping": _money(r.get("total_shipping_cost")),
                    "order_sales_tax": _money(r.get("total_tax_cost")),
                    "order_discount": _money(r.get("discount_amt")),
                    "order_total": _money(r.get("grandtotal")),
                    "currency": (r.get("grandtotal") or {}).get("currency_code", "USD"),
                    "date_paid": sale_date if r.get("is_paid") else "",
                    "date_shipped": sale_date if r.get("is_shipped") else "",
                })
                for t in r.get("transactions", []):
                    qty = t.get("quantity") or 1
                    unit = _money(t.get("price"))
                    items.append({
                        "transaction_id": str(t.get("transaction_id")),
                        "order_id": oid,
                        "listing_id": str(t.get("listing_id") or ""),
                        "sku": (t.get("sku") or "").strip(),
                        "item_name": t.get("title", ""),
                        "variations": "; ".join(
                            f"{v.get('formatted_name', '')}:{v.get('formatted_value', '')}"
                            for v in (t.get("variations") or [])),
                        "quantity": qty,
                        "price": unit,
                        "item_total": round(unit * qty, 2),
                        "sale_date": sale_date,
                    })
            count = data.get("count", 0) if isinstance(data, dict) else 0
            offset += limit
            if not results or offset >= count:
                break
            time.sleep(0.2)
        return orders, items

    def sync(self, settings):
        if not self.is_configured(settings):
            raise RuntimeError("Etsy connector not configured — run get_etsy_token.py.")

        # Network first (so the DB isn't held open across API calls)...
        token = self._access_token(settings)
        orders, items = self._collect(settings, token)

        # ...then write in one short transaction.
        conn = get_db()
        now = datetime.now().isoformat(timespec="seconds")
        new = updated = 0
        try:
            for o in orders:
                conn.execute(
                    """INSERT INTO etsy_orders
                       (order_id, channel, sale_date, buyer, order_shipping,
                        order_sales_tax, order_discount, order_total, currency,
                        date_paid, date_shipped)
                       VALUES (?,'etsy',?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(order_id) DO UPDATE SET
                         sale_date=COALESCE(NULLIF(excluded.sale_date,''), sale_date),
                         buyer=COALESCE(NULLIF(excluded.buyer,''), buyer),
                         order_shipping=excluded.order_shipping,
                         order_sales_tax=excluded.order_sales_tax,
                         order_discount=excluded.order_discount,
                         order_total=excluded.order_total,
                         date_paid=COALESCE(NULLIF(excluded.date_paid,''), date_paid),
                         date_shipped=COALESCE(NULLIF(excluded.date_shipped,''), date_shipped)""",
                    (o["order_id"], o["sale_date"], o["buyer"], o["order_shipping"],
                     o["order_sales_tax"], o["order_discount"], o["order_total"],
                     o["currency"], o["date_paid"], o["date_shipped"]))
            for it in items:
                exists = conn.execute(
                    "SELECT 1 FROM etsy_items WHERE transaction_id=?",
                    (it["transaction_id"],)).fetchone()
                conn.execute(
                    """INSERT INTO etsy_items
                       (transaction_id, channel, order_id, listing_id, sku,
                        item_name, variations, quantity, price, discount_amount,
                        item_total, sale_date)
                       VALUES (?,'etsy',?,?,?,?,?,?,?,0,?,?)
                       ON CONFLICT(transaction_id) DO UPDATE SET
                         order_id=excluded.order_id,
                         listing_id=excluded.listing_id,
                         sku=excluded.sku,
                         item_name=excluded.item_name,
                         variations=excluded.variations,
                         quantity=excluded.quantity,
                         price=excluded.price,
                         item_total=excluded.item_total,
                         sale_date=excluded.sale_date""",
                    (it["transaction_id"], it["order_id"], it["listing_id"],
                     it["sku"], it["item_name"], it["variations"], it["quantity"],
                     it["price"], it["item_total"], it["sale_date"]))
                new += 0 if exists else 1
                updated += 1 if exists else 0
            conn.execute(
                """INSERT INTO import_log
                   (source, filename, rows_seen, rows_new, rows_updated, imported_at)
                   VALUES (?,?,?,?,?,?)""",
                ("etsy-api", "API sync (receipts)", len(items), new, updated, now))
            conn.commit()
        finally:
            conn.close()
        return {"rows_seen": len(items), "rows_new": new,
                "rows_updated": updated, "orders": len(orders)}
