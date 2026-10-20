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


def _history():
    """Per-day and per-product totals so the page can total any date range.

    Uses analytics' own per-item cost and fee math; the fixed per-order fee is
    added to the day the order was placed, as analytics.summary() does.
    """
    settings = get_settings()
    pct, listing_fee, fixed = analytics._fee_rates(settings)
    conn = get_db()
    rows = analytics._rows(conn, None, None)
    conn.close()

    days, products, product_ids, product_days = {}, [], {}, {}
    orders_by_day = {}
    for r in rows:
        date_ = r["sale_date"] or ""
        if not date_:
            continue
        rev, cost, fee, mapped = analytics._item_calc(r, pct, listing_fee)
        units = r["quantity"] or 0
        d = days.setdefault(date_, {"date": date_, "revenue": 0, "cost": 0, "fees": 0,
                                    "orders": 0, "units": 0, "unmapped_units": 0,
                                    "shipped": 0, "paid": 0, "unpaid": 0})
        d["revenue"] += rev; d["cost"] += cost; d["fees"] += fee; d["units"] += units
        if not mapped:
            d["unmapped_units"] += units
        orders_by_day.setdefault(date_, set()).add(r["order_id"])

        key = r["listing_id"] or r["item_name"] or "?"
        if key not in product_ids:
            product_ids[key] = len(products)
            products.append(r["item_name"] or "(untitled)")
        p = product_days.setdefault((date_, product_ids[key]), [0, 0, 0])
        p[0] += rev; p[1] += rev - cost - fee; p[2] += units

    for date_, ids in orders_by_day.items():
        days[date_]["orders"] = len(ids)
        days[date_]["fees"] += fixed * len(ids)
    for r in _orders():
        if r["sale_date"] in days:
            days[r["sale_date"]][_status(r)] += 1

    for d in days.values():
        for k in ("revenue", "cost", "fees"):
            d[k] = round(d[k], 2)

    orders = [{"date": r["sale_date"], "products": _products(r["names"]),
               "units": r["units"], "status": _status(r),
               "amount": round(r["order_total"] or 0, 2)}
              for r in _orders()]

    return {
        "days": [days[k] for k in sorted(days)],
        "products": products,
        # [date, product index, revenue, profit, units]
        "product_days": [[date_, i, round(v[0], 2), round(v[1], 2), v[2]]
                         for (date_, i), v in sorted(product_days.items())],
        "orders": orders,
    }


def _tax_orders():
    """Per-order money and ship-to state for the sales tax worksheets.

    City and ZIP are kept only for Louisiana orders (needed to find the
    parish); no order ids or buyer details.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT sale_date, ship_state, ship_country, ship_city, ship_zip, item_value,
               order_discount, order_shipping, order_sales_tax, order_total, refund_total
        FROM etsy_orders
        ORDER BY sale_date DESC, order_id DESC""").fetchall()
    conn.close()
    out = []
    for r in rows:
        state = (r["ship_state"] or "").strip()
        o = {"date": r["sale_date"], "state": state,
             "country": (r["ship_country"] or "").strip()}
        if state.upper() in ("LA", "LOUISIANA"):
            o["city"] = (r["ship_city"] or "").strip()
            o["zip"] = (r["ship_zip"] or "").strip()[:5]
        o.update({k: round(r[col] or 0, 2) for k, col in (
            ("items", "item_value"), ("discount", "order_discount"),
            ("shipping", "order_shipping"), ("tax", "order_sales_tax"),
            ("total", "order_total"), ("refunds", "refund_total"))})
        out.append(o)
    return out


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
        "history": _history(),
        "tax": {"orders": _tax_orders()},
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
