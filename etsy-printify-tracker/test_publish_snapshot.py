"""Tests for publish_snapshot.py. Run from the app folder:  python -m pytest

Each test builds a throwaway tracker.db with hand-picked sales, so every
expected number below was worked out by hand from the fixture.
Fee model = the app defaults: 6.5% + 3.0% of revenue, $0.20 per item line,
$0.25 per order.
"""
import json
from datetime import date

import pytest

import database
import publish_snapshot

TODAY = date(2026, 10, 20)   # 28-day window = 2026-09-23 .. 2026-10-20

SECRETS = {"etsy_shared_secret": "SHHH-secret-123",
           "etsy_refresh_token": "rt-abc-999",
           "printify_token": "pf-token-777"}

# (order_id, sale_date, buyer, order_total, date_paid, date_shipped)
ORDERS = [
    ("3141592601", "2026-10-19", "Jane Doe", 30.50, "2026-10-19", "2026-10-19"),
    ("3141592602", "2026-10-01", "Bob Smith", 40.00, "2026-10-01", ""),
    ("3141592603", "2026-10-10", "Cara Lee", 23.00, "", ""),
    ("3141592604", "2026-09-01", "Dan Roe", 26.00, "2026-09-01", "2026-09-01"),
    ("3141592605", "2026-06-15", "Eve Poe", 18.50, "2026-06-15", "2026-06-15"),
    ("3141592606", "2025-01-05", "Fay Fox", 26.00, "2025-01-05", "2025-01-05"),
]

# (transaction_id, order_id, listing_id, sku, item_name, variations, qty, item_total, sale_date)
ITEMS = [
    ("9000000001", "3141592601", "1111111111", "TEE-1", "Heron Tee", "Size:L", 1, 26.00, "2026-10-19"),
    ("9000000002", "3141592602", "2222222222", "", "Bayou Mug", "Personalization:For Jane", 2, 37.00, "2026-10-01"),
    ("9000000003", "3141592603", "3333333333", "", "Crawfish Poster", "", 1, 23.00, "2026-10-10"),
    ("9000000004", "3141592604", "1111111111", "TEE-1", "Heron Tee", "Size:M", 1, 26.00, "2026-09-01"),
    ("9000000005", "3141592605", "2222222222", "", "Bayou Mug", "", 1, 18.50, "2026-06-15"),
    ("9000000006", "3141592606", "1111111111", "TEE-1", "Heron Tee", "", 1, 26.00, "2025-01-05"),
]


@pytest.fixture(autouse=True)
def tracker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "tracker.db"))
    database.init_db()
    database.save_settings(SECRETS)
    conn = database.get_db()
    conn.executemany(
        """INSERT INTO etsy_orders (order_id, sale_date, buyer, order_total,
           date_paid, date_shipped) VALUES (?,?,?,?,?,?)""", ORDERS)
    conn.executemany(
        """INSERT INTO etsy_items (transaction_id, order_id, listing_id, sku,
           item_name, variations, quantity, item_total, sale_date)
           VALUES (?,?,?,?,?,?,?,?,?)""", ITEMS)
    # Heron Tee costs by SKU, Bayou Mug by listing (Printify); poster has none.
    conn.execute("INSERT INTO sku_costs (sku, unit_cost, ship_cost) "
                 "VALUES ('TEE-1', 10, 4)")
    conn.execute("INSERT INTO listing_costs (listing_id, unit_cost, ship_cost) "
                 "VALUES ('2222222222', 6, 3)")
    conn.commit()
    conn.close()


def snap():
    return publish_snapshot.build_snapshot(today=TODAY)


def test_kpis_cover_the_last_28_days_with_real_fee_and_cost_math():
    k = snap()["kpis"]
    # revenue 26 + 37 + 23; cost (10+4)*1 + (6+3)*2;
    # fees 86*9.5% + 3*0.20 + 3*0.25 = 8.17 + 0.60 + 0.75
    assert k["revenue"]["value"] == pytest.approx(86.00)
    assert k["orders"]["value"] == 3
    assert k["avg_order_value"]["value"] == pytest.approx(28.67)
    assert k["product_cost"]["value"] == pytest.approx(32.00)
    assert k["fees"]["value"] == pytest.approx(9.52)
    assert k["profit"]["value"] == pytest.approx(44.48)
    assert k["margin"]["value"] == pytest.approx(51.7)


def test_kpis_carry_the_prior_28_days_for_comparison():
    k = snap()["kpis"]
    # prior window 2026-08-26 .. 2026-09-22 holds only the 09-01 tee:
    # fees 26*9.5% + 0.20 + 0.25 = 2.92; profit 26 - 14 - 2.92
    assert k["revenue"]["prior"] == pytest.approx(26.00)
    assert k["orders"]["prior"] == 1
    assert k["profit"]["prior"] == pytest.approx(9.08)


def test_window_and_daily_revenue_fill_every_day():
    s = snap()
    assert s["window"] == {"start": "2026-09-23", "end": "2026-10-20", "days": 28}
    daily = s["daily_revenue"]
    assert len(daily) == 28
    assert daily[0]["date"] == "2026-09-23"
    assert daily[-1]["date"] == "2026-10-20"
    by_day = {d["date"]: d["revenue"] for d in daily}
    assert by_day["2026-10-19"] == pytest.approx(26.00)
    assert by_day["2026-10-01"] == pytest.approx(37.00)
    assert by_day["2026-10-10"] == pytest.approx(23.00)
    assert by_day["2026-10-20"] == 0


def test_monthly_series_keeps_only_the_last_12_months():
    months = [m["month"] for m in snap()["monthly"]]
    assert months == ["2026-06", "2026-09", "2026-10"]


def test_top_products_rank_window_sales_by_revenue():
    top = snap()["top_products"]
    assert [p["name"] for p in top] == ["Bayou Mug", "Heron Tee", "Crawfish Poster"]
    assert [p["units"] for p in top] == [2, 1, 1]
    # mug: 37 - 18 cost - (37*9.5% + 0.20)
    assert top[0]["profit"] == pytest.approx(15.29, abs=0.01)


def test_fulfillment_counts_window_orders_by_status():
    assert snap()["fulfillment"] == {"shipped": 1, "paid": 1, "unpaid": 1}


def test_recent_orders_are_newest_first_without_identifying_fields():
    recent = snap()["recent_orders"]
    assert len(recent) == 6
    assert recent[0] == {"date": "2026-10-19", "products": "Heron Tee",
                         "units": 1, "status": "shipped", "amount": 30.50}
    assert [o["status"] for o in recent[:3]] == ["shipped", "unpaid", "paid"]


def test_unmapped_units_flag_sales_with_no_cost_on_file():
    assert snap()["unmapped_units"] == 1


def test_snapshot_never_leaks_buyers_ids_variations_or_keys():
    blob = json.dumps(snap())
    for name in ("Jane", "Bob Smith", "Cara Lee", "Dan Roe", "Eve Poe", "Fay Fox"):
        assert name not in blob
    for order_id, *_ in ORDERS:
        assert order_id not in blob
    for txn, _, listing, *_ in ITEMS:
        assert txn not in blob and listing not in blob
    assert "TEE-1" not in blob and "Personalization" not in blob
    for secret in SECRETS.values():
        assert secret not in blob


def test_write_snapshot_saves_data_json(tmp_path):
    out = publish_snapshot.write_snapshot(snap(), tmp_path)
    assert out == tmp_path / "data.json"
    assert json.loads(out.read_text())["kpis"]["orders"]["value"] == 3


# ---- history: lets the page total any date range the viewer picks ----------

def day(s, date_):
    return next(d for d in s["history"]["days"] if d["date"] == date_)


def test_history_days_hold_each_days_totals():
    s = snap()
    days = s["history"]["days"]
    assert [d["date"] for d in days] == sorted(o[1] for o in ORDERS)  # sales days only
    mug = day(s, "2026-10-01")
    # 37 revenue; cost (6+3)*2; fees 37*9.5% + 0.20 + 0.25 per-order
    assert mug["revenue"] == pytest.approx(37.00)
    assert mug["cost"] == pytest.approx(18.00)
    assert mug["fees"] == pytest.approx(3.97, abs=0.011)
    assert (mug["orders"], mug["units"], mug["unmapped_units"]) == (1, 2, 0)
    assert (mug["shipped"], mug["paid"], mug["unpaid"]) == (0, 1, 0)
    poster = day(s, "2026-10-10")
    assert (poster["unmapped_units"], poster["unpaid"]) == (1, 1)


def test_history_days_add_up_to_the_window_kpis():
    s = snap()
    w = [d for d in s["history"]["days"] if "2026-09-23" <= d["date"] <= "2026-10-20"]
    k = s["kpis"]
    assert sum(d["revenue"] for d in w) == pytest.approx(k["revenue"]["value"])
    assert sum(d["cost"] for d in w) == pytest.approx(k["product_cost"]["value"])
    assert sum(d["fees"] for d in w) == pytest.approx(k["fees"]["value"], abs=0.011)
    assert sum(d["orders"] for d in w) == k["orders"]["value"]


def test_history_product_days_attribute_sales_to_named_products():
    h = snap()["history"]
    names = h["products"]
    assert sorted(names) == ["Bayou Mug", "Crawfish Poster", "Heron Tee"]
    rows = {(r[0], names[r[1]]): r[2:] for r in h["product_days"]}
    revenue, profit, units = rows[("2026-10-01", "Bayou Mug")]
    assert (revenue, units) == (37.00, 2)
    assert profit == pytest.approx(15.29, abs=0.01)   # 37 - 18 - (3.515 + 0.20)
    assert sorted(d for d, n in rows if n == "Heron Tee") == ["2025-01-05", "2026-09-01", "2026-10-19"]


def test_history_orders_list_every_order_newest_first():
    orders = snap()["history"]["orders"]
    assert len(orders) == len(ORDERS)
    assert orders[0] == {"date": "2026-10-19", "products": "Heron Tee",
                         "units": 1, "status": "shipped", "amount": 30.50}
    assert orders[-1]["date"] == "2025-01-05"


# ---- sales tax: per-order detail the page builds worksheets from -----------

@pytest.fixture
def ship_to():
    conn = database.get_db()
    rows = [  # order_id, state, country, city, zip, item_value, discount, shipping, tax, refunds
        ("3141592601", "LA", "US", "Baton Rouge", "70808", 26.00, 0.00, 2.00, 2.50, 0.00),
        ("3141592602", "TX", "US", "Austin", "73301", 37.00, 0.00, 3.00, 0.00, 0.00),
        ("3141592603", "LA", "US", "New Orleans", "70115", 25.00, 2.00, 0.00, 0.00, 5.00),
    ]
    for oid, st, co, city, zip_, items, disc, ship, tax, refunds in rows:
        conn.execute("""UPDATE etsy_orders SET ship_state=?, ship_country=?, ship_city=?,
                        ship_zip=?, item_value=?, order_discount=?, order_shipping=?,
                        order_sales_tax=?, refund_total=? WHERE order_id=?""",
                     (st, co, city, zip_, items, disc, ship, tax, refunds, oid))
    conn.commit(); conn.close()


def test_tax_orders_carry_ship_state_and_money_fields(ship_to):
    orders = snap()["tax"]["orders"]
    assert len(orders) == len(ORDERS)
    assert [o["date"] for o in orders] == sorted((o[1] for o in ORDERS), reverse=True)
    assert orders[0] == {"date": "2026-10-19", "state": "LA", "country": "US",
                         "city": "Baton Rouge", "zip": "70808", "items": 26.00,
                         "discount": 0.00, "shipping": 2.00, "tax": 2.50,
                         "total": 30.50, "refunds": 0.00}
    poster = next(o for o in orders if o["date"] == "2026-10-10")
    assert (poster["discount"], poster["refunds"], poster["tax"]) == (2.00, 5.00, 0.00)


def test_tax_orders_give_city_and_zip_only_for_louisiana(ship_to):
    orders = snap()["tax"]["orders"]
    texas = next(o for o in orders if o["state"] == "TX")
    assert "city" not in texas and "zip" not in texas
    blob = json.dumps(snap())
    assert "Austin" not in blob and "73301" not in blob


def test_tax_orders_without_ship_data_are_kept_with_blank_state():
    orders = snap()["tax"]["orders"]       # fixture orders have no ship_to here
    assert len(orders) == len(ORDERS)
    assert all(o["state"] == "" for o in orders)
