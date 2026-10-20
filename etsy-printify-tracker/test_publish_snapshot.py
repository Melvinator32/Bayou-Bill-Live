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
