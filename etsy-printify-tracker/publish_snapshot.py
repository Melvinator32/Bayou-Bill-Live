r"""Publish a public, privacy-safe snapshot of the tracker's numbers.

Reads tracker.db through analytics.py (same fee model and cost matching as the
dashboard) and writes data.json for the public Bayou Bill live page. Only
aggregates and product titles leave this machine: no buyer names, order /
transaction / listing ids, SKUs, variations (they can hold personalization
text), or settings (they hold the Etsy and Printify keys).

Run from the app folder:
    python publish_snapshot.py                         # writes public_snapshot\data.json
    python publish_snapshot.py --sync                  # sync connectors first
    python publish_snapshot.py --sync --repo ..
        # writes data.json at the repo root (next to index.html), commits, pushes
"""
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import analytics
from database import get_db, get_settings, init_db

WINDOW_DAYS = 28
MONTHS = 12
TOP_PRODUCTS = 6
RECENT_ORDERS = 10


def _window(end, days=WINDOW_DAYS):
    start = end - timedelta(days=days - 1)
    return start, end


def _daily_revenue(start, end):
    conn = get_db()
    rows = conn.execute(
        """SELECT sale_date, SUM(item_total) AS revenue FROM etsy_items
           WHERE sale_date >= ? AND sale_date <= ? GROUP BY sale_date""",
        (start.isoformat(), end.isoformat())).fetchall()
    conn.close()
    by_day = {r["sale_date"]: r["revenue"] or 0 for r in rows}
    out, d = [], start
    while d <= end:
        out.append({"date": d.isoformat(),
                    "revenue": round(by_day.get(d.isoformat(), 0), 2)})
        d += timedelta(days=1)
    return out


def _status(row):
    if row["date_shipped"]:
        return "shipped"
    if row["date_paid"]:
        return "paid"
    return "unpaid"


def _orders(start=None, end=None, limit=None):
    clauses, params = [], []
    if start:
        clauses.append("o.sale_date >= ?"); params.append(start.isoformat())
    if end:
        clauses.append("o.sale_date <= ?"); params.append(end.isoformat())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT o.order_id, o.sale_date, o.order_total, o.date_paid, o.date_shipped,
               GROUP_CONCAT(i.item_name, '||') AS names,
               COALESCE(SUM(i.quantity), 0) AS units
        FROM etsy_orders o
        LEFT JOIN etsy_items i ON i.order_id = o.order_id
        {where}
        GROUP BY o.order_id
        ORDER BY o.sale_date DESC, o.order_id DESC"""
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def _products(names):
    seen = []
    for n in (names or "").split("||"):
        if n and n not in seen:
            seen.append(n)
    return ", ".join(seen) or "(untitled)"


def build_snapshot(today=None):
    today = today or date.today()
    start, end = _window(today)
    p_start, p_end = _window(start - timedelta(days=1))

    cur = analytics.summary(start.isoformat(), end.isoformat())
    prior = analytics.summary(p_start.isoformat(), p_end.isoformat())

    def aov(s):
        return round(s["revenue"] / s["orders"], 2) if s["orders"] else 0

    def kpi(key):
        return {"value": cur[key], "prior": prior[key]}

    kpis = {
        "revenue": kpi("revenue"),
        "orders": kpi("orders"),
        "avg_order_value": {"value": aov(cur), "prior": aov(prior)},
        "product_cost": kpi("product_cost"),
        "fees": kpi("fees"),
        "profit": kpi("profit"),
        "margin": kpi("margin"),
    }

    month_start = date(today.year, today.month, 1)
    for _ in range(MONTHS - 1):
        month_start = (month_start - timedelta(days=1)).replace(day=1)
    monthly = analytics.monthly_series(month_start.isoformat(), end.isoformat())

    top = [{"name": p["name"], "revenue": p["revenue"], "profit": p["profit"],
            "units": p["units"]}
           for p in analytics.by_listing(start.isoformat(), end.isoformat(),
                                         limit=TOP_PRODUCTS)]

    fulfillment = {"shipped": 0, "paid": 0, "unpaid": 0}
    for r in _orders(start, end):
        fulfillment[_status(r)] += 1

    recent = [{"date": r["sale_date"], "products": _products(r["names"]),
               "units": r["units"], "status": _status(r),
               "amount": round(r["order_total"] or 0, 2)}
              for r in _orders(limit=RECENT_ORDERS)]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "currency": get_settings().get("currency", "USD"),
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": WINDOW_DAYS},
        "kpis": kpis,
        "daily_revenue": _daily_revenue(start, end),
        "monthly": monthly,
        "top_products": top,
        "fulfillment": fulfillment,
        "recent_orders": recent,
        "unmapped_units": cur["unmapped_units"],
    }


def write_snapshot(snapshot, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "data.json"
    out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return out


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sync", action="store_true",
                    help="run every configured connector sync first")
    ap.add_argument("--repo", help="clone of the public site repo; commits and pushes data.json")
    args = ap.parse_args(argv)

    init_db()
    if args.sync:
        import sync
        sync.main()

    out = write_snapshot(build_snapshot(), args.repo or "public_snapshot")
    print(f"Wrote {out}")

    if args.repo:
        # generated_at changes every run, so there is always something to commit;
        # that keeps the page's "Updated" time honest.
        _git(args.repo, "add", "data.json")
        _git(args.repo, "commit", "-m", f"Update snapshot {date.today().isoformat()}")
        _git(args.repo, "push")
        print("Pushed. The live page updates in a minute or two.")


if __name__ == "__main__":
    sys.exit(main())
