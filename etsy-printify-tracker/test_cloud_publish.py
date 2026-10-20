"""Tests for cloud_publish.py (the scheduled GitHub job). Run from this folder:
    python -m pytest test_cloud_publish.py

Connectors are replaced with in-memory fakes, so nothing touches Etsy or
Printify. Fee model = app defaults (6.5% + 3.0%, $0.20/line, $0.25/order).
"""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import cloud_publish
import database

TODAY = date(2026, 10, 20)

ENV = {
    "ETSY_KEYSTRING": "ks-123",
    "ETSY_SHARED_SECRET": "shh-456",
    "ETSY_REFRESH_TOKEN": "rt-789",
    "ETSY_SHOP_ID": "55555",
    "PRINTIFY_TOKEN": "pf-000",
    "PRINTIFY_SHOP_ID": "",          # blank: connector auto-detects
    "HOME": "/home/runner",          # unrelated variables are ignored
}


class FakeEtsy:
    key, label, kind, implemented = "etsy_api", "Etsy (API)", "sales", True

    def __init__(self, fail=False):
        self.fail, self.seen = fail, None

    def is_configured(self, settings):
        return bool(settings.get("etsy_refresh_token"))

    def sync(self, settings):
        self.seen = dict(settings)
        if self.fail:
            raise RuntimeError("401 from Etsy")
        conn = database.get_db()
        conn.execute("""INSERT INTO etsy_orders (order_id, sale_date, buyer, order_total,
                        date_paid, date_shipped)
                        VALUES ('3141592601','2026-10-19','Jane Doe',30.50,'2026-10-19','')""")
        conn.execute("""INSERT INTO etsy_items (transaction_id, order_id, listing_id, sku,
                        item_name, quantity, item_total, sale_date)
                        VALUES ('9001','3141592601','1111111111','TEE-1','Heron Tee',1,26.00,'2026-10-19')""")
        conn.commit(); conn.close()
        return {"rows_seen": 1, "rows_new": 1, "rows_updated": 0}


class FakePrintify:
    key, label, kind, implemented = "printify", "Printify (costs)", "cost", True

    def __init__(self, fail=False):
        self.fail = fail

    def is_configured(self, settings):
        return bool(settings.get("printify_token"))

    def sync(self, settings):
        if self.fail:
            raise RuntimeError("Printify 500")
        conn = database.get_db()
        conn.execute("INSERT INTO sku_costs (sku, unit_cost, ship_cost) VALUES ('TEE-1', 10, 4)")
        conn.commit(); conn.close()
        return {"rows_seen": 1, "rows_new": 1, "rows_updated": 0}


class Unbuilt:
    key, label, kind, implemented = "ebay_api", "eBay (API)", "sales", False

    def is_configured(self, settings):
        return True

    def sync(self, settings):
        raise AssertionError("unimplemented connectors must never run")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", database.DB_PATH)  # restored after test
    out = tmp_path / "site"
    out.mkdir()
    return SimpleNamespace(db=tmp_path / "scratch" / "tracker.db", out=out)


def run(paths, env=ENV, **conns):
    return cloud_publish.run(env, out_dir=paths.out, db_path=paths.db,
                             connectors=conns, today=TODAY)


def test_settings_come_from_env_and_skip_blanks_and_unrelated_vars():
    assert cloud_publish.settings_from_env(ENV) == {
        "etsy_keystring": "ks-123",
        "etsy_shared_secret": "shh-456",
        "etsy_refresh_token": "rt-789",
        "etsy_shop_id": "55555",
        "printify_token": "pf-000",
    }


def test_fee_overrides_come_from_env():
    s = cloud_publish.settings_from_env({"TRANSACTION_FEE_PCT": "6.5",
                                         "OFFSITE_ADS_PCT": "15"})
    assert s == {"transaction_fee_pct": "6.5", "offsite_ads_pct": "15"}


def test_run_syncs_then_publishes_real_numbers(paths):
    etsy = FakeEtsy()
    run(paths, etsy=etsy, printify=FakePrintify(), ebay=Unbuilt())

    assert etsy.seen["etsy_refresh_token"] == "rt-789"
    k = json.loads((paths.out / "data.json").read_text())["kpis"]
    # 26 revenue; cost 10+4; fees 26*9.5% + 0.20 + 0.25 = 2.92
    assert k["revenue"]["value"] == pytest.approx(26.00)
    assert k["product_cost"]["value"] == pytest.approx(14.00)
    assert k["profit"]["value"] == pytest.approx(9.08)


def test_published_file_holds_no_keys_or_buyers(paths):
    run(paths, etsy=FakeEtsy(), printify=FakePrintify())
    blob = (paths.out / "data.json").read_text()
    for value in ("ks-123", "shh-456", "rt-789", "pf-000", "55555", "Jane Doe", "3141592601"):
        assert value not in blob


def test_scratch_database_is_written_outside_the_site_folder(paths):
    run(paths, etsy=FakeEtsy(), printify=FakePrintify())
    assert paths.db.exists()
    assert sorted(p.name for p in paths.out.iterdir()) == ["data.json"]


def test_failed_sync_keeps_the_last_published_numbers(paths):
    (paths.out / "data.json").write_text('{"old": true}')
    with pytest.raises(cloud_publish.PublishError, match="Etsy"):
        run(paths, etsy=FakeEtsy(fail=True), printify=FakePrintify())
    assert json.loads((paths.out / "data.json").read_text()) == {"old": True}


def test_failed_cost_sync_also_blocks_publishing(paths):
    with pytest.raises(cloud_publish.PublishError, match="Printify"):
        run(paths, etsy=FakeEtsy(), printify=FakePrintify(fail=True))
    assert not (paths.out / "data.json").exists()


def test_missing_sales_credentials_block_publishing(paths):
    env = {k: v for k, v in ENV.items() if not k.startswith("ETSY_")}
    with pytest.raises(cloud_publish.PublishError, match="sales"):
        run(paths, env=env, etsy=FakeEtsy(), printify=FakePrintify())
    assert not (paths.out / "data.json").exists()


def test_rotated_etsy_refresh_token_is_captured():
    calls = []

    def fake_post(url, data=None, **kw):
        calls.append(url)
        return SimpleNamespace(json=lambda: {"access_token": "at", "refresh_token": "rt-NEW"})

    target = SimpleNamespace(post=fake_post)
    seen = []
    with cloud_publish.capture_refresh_tokens(target, seen):
        target.post(cloud_publish.ETSY_TOKEN_URL, data={"grant_type": "refresh_token"})
        target.post("https://api.printify.com/v1/shops.json")
    assert seen == ["rt-NEW"]
    assert target.post is fake_post          # restored afterwards
    assert len(calls) == 2


def test_failure_message_includes_the_api_error_body(paths):
    import requests

    class RejectedEtsy(FakeEtsy):
        def sync(self, settings):
            resp = SimpleNamespace(
                status_code=400,
                text='{"error":"invalid_grant","error_description":"refresh token expired"}')
            raise requests.exceptions.HTTPError("400 Client Error: Bad Request", response=resp)

    with pytest.raises(cloud_publish.PublishError, match="invalid_grant.*refresh token expired"):
        run(paths, etsy=RejectedEtsy(), printify=FakePrintify())
