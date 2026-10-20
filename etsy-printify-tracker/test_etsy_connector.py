"""Tests for the Etsy connector's receipt parsing and storage. No network:
_get and _access_token are replaced with canned Etsy API v3 responses.
    python -m pytest test_etsy_connector.py
"""
import pytest

import database
from connectors.etsy import EtsyConnector

SETTINGS = {"etsy_keystring": "ks", "etsy_shared_secret": "ss",
            "etsy_refresh_token": "rt", "etsy_shop_id": "123"}


def money(cents):
    return {"amount": cents, "divisor": 100, "currency_code": "USD"}


# Trimmed from a real ShopReceipt: Louisiana buyer, one partial refund.
RECEIPT = {
    "receipt_id": 3300000001,
    "name": "Jane Doe",
    "city": "Baton Rouge",
    "state": "LA",
    "zip": "70808-1234",
    "country_iso": "US",
    "is_paid": True,
    "is_shipped": True,
    "created_timestamp": 1760918400,          # 2025-10-20 UTC
    "total_price": money(5000),               # items before discount
    "discount_amt": money(500),
    "total_shipping_cost": money(600),
    "total_tax_cost": money(481),
    "grandtotal": money(5581),
    "refunds": [{"amount": money(1000), "reason": "damaged"}],
    "transactions": [{"transaction_id": 4400000001, "listing_id": 55, "sku": "TEE-1",
                      "title": "Heron Tee", "quantity": 2, "price": money(2500),
                      "variations": []}],
}


@pytest.fixture
def etsy(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "tracker.db"))
    database.init_db()
    c = EtsyConnector()
    monkeypatch.setattr(c, "_access_token", lambda settings: "at")
    monkeypatch.setattr(c, "_get", lambda path, settings, token: {"results": [RECEIPT], "count": 1})
    return c


def test_collect_reads_ship_to_and_money_fields(etsy):
    orders, _ = etsy._collect(SETTINGS, "at")
    o = orders[0]
    assert (o["ship_state"], o["ship_country"], o["ship_city"], o["ship_zip"]) == \
        ("LA", "US", "Baton Rouge", "70808")
    assert o["item_value"] == 50.00
    assert o["order_discount"] == 5.00
    assert o["refund_total"] == 10.00


def test_sync_stores_ship_to_and_refunds(etsy):
    etsy.sync(SETTINGS)
    conn = database.get_db()
    row = conn.execute("""SELECT ship_state, ship_country, ship_city, ship_zip,
                                 item_value, refund_total, order_sales_tax
                          FROM etsy_orders WHERE order_id = '3300000001'""").fetchone()
    conn.close()
    assert tuple(row) == ("LA", "US", "Baton Rouge", "70808", 50.00, 10.00, 4.81)


def test_old_databases_gain_the_new_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    conn = database.get_db()
    conn.execute("CREATE TABLE etsy_orders (order_id TEXT PRIMARY KEY, sale_date TEXT)")
    conn.commit(); conn.close()
    database.init_db()
    conn = database.get_db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(etsy_orders)")}
    conn.close()
    assert {"ship_state", "ship_country", "ship_city", "ship_zip",
            "item_value", "refund_total"} <= cols
