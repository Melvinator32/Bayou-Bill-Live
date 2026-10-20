"""Profit analytics: joins sold items to SKU costs, applies the Etsy fee model,
and produces the numbers the dashboard shows.

Profit per item = item revenue
                  - product cost (unit_cost * qty + ship_cost)
                  - estimated Etsy fees (% model from Settings)
Plus the fixed per-ORDER processing fee, applied once per order in summary().

Etsy fees are ESTIMATES (Etsy's data has no full fee breakdown). Printify costs,
once synced, are real.
"""
from database import get_db, get_settings


def _fee_rates(settings):
    g = lambda k: float(settings.get(k, 0) or 0)
    pct = (g("transaction_fee_pct") + g("processing_fee_pct")
           + g("offsite_ads_pct")) / 100.0
    return pct, g("listing_fee"), g("processing_fee_fixed")


def _where(start, end):
    clauses, params = [], []
    if start:
        clauses.append("i.sale_date >= ?"); params.append(start)
    if end:
        clauses.append("i.sale_date <= ?"); params.append(end)
    return (("WHERE " + " AND ".join(clauses)) if clauses else ""), params


def _rows(conn, start, end):
    where, params = _where(start, end)
    return conn.execute(f"""
        SELECT i.transaction_id, i.order_id, i.listing_id, i.sku, i.item_name,
               i.quantity, i.item_total, i.sale_date,
               c.unit_cost  AS sku_unit,  c.ship_cost  AS sku_ship,
               lc.unit_cost AS lst_unit,  lc.ship_cost AS lst_ship
        FROM etsy_items i
        LEFT JOIN sku_costs c
               ON c.sku = i.sku AND i.sku != ''
        LEFT JOIN listing_costs lc
               ON lc.listing_id != ''
              AND (lc.listing_id = i.listing_id OR lc.listing_id = i.sku)
        {where}
    """, params).fetchall()


def _item_calc(r, pct, listing_fee):
    revenue = r["item_total"] or 0
    qty = r["quantity"] or 1
    # Prefer a SKU-keyed cost; fall back to a listing-keyed cost (Printify).
    if r["sku_unit"] is not None:
        unit, ship, mapped = r["sku_unit"], (r["sku_ship"] or 0), True
    elif r["lst_unit"] is not None:
        unit, ship, mapped = r["lst_unit"], (r["lst_ship"] or 0), True
    else:
        unit, ship, mapped = 0, 0, False
    cost = (unit + ship) * qty if mapped else 0
    fee = revenue * pct + listing_fee
    return revenue, cost, fee, mapped


def date_bounds():
    conn = get_db()
    row = conn.execute(
        "SELECT MIN(sale_date) mn, MAX(sale_date) mx FROM etsy_items").fetchone()
    conn.close()
    return (row["mn"], row["mx"]) if row else (None, None)


def summary(start=None, end=None):
    settings = get_settings()
    pct, listing_fee, fixed = _fee_rates(settings)
    conn = get_db()
    rows = _rows(conn, start, end)
    where, params = _where(start, end)
    n_orders = conn.execute(
        f"SELECT COUNT(DISTINCT i.order_id) n FROM etsy_items i {where}",
        params).fetchone()["n"]
    conn.close()

    revenue = cost = fees = units = unmapped_units = 0
    for r in rows:
        rev, c, fee, mapped = _item_calc(r, pct, listing_fee)
        revenue += rev; cost += c; fees += fee
        units += r["quantity"] or 0
        if not mapped:
            unmapped_units += r["quantity"] or 0
    fees += fixed * n_orders
    profit = revenue - cost - fees
    margin = (profit / revenue * 100) if revenue else 0
    return {
        "revenue": round(revenue, 2), "product_cost": round(cost, 2),
        "fees": round(fees, 2), "profit": round(profit, 2),
        "margin": round(margin, 1), "orders": n_orders, "units": units,
        "unmapped_units": unmapped_units, "items": len(rows),
    }


def monthly_series(start=None, end=None):
    settings = get_settings()
    pct, listing_fee, _ = _fee_rates(settings)
    conn = get_db()
    rows = _rows(conn, start, end)
    conn.close()
    buckets = {}
    for r in rows:
        month = (r["sale_date"] or "")[:7]
        if not month:
            continue
        rev, c, fee, _ = _item_calc(r, pct, listing_fee)
        b = buckets.setdefault(month, {"revenue": 0, "profit": 0})
        b["revenue"] += rev
        b["profit"] += rev - c - fee
    return [{"month": m, "revenue": round(v["revenue"], 2),
             "profit": round(v["profit"], 2)}
            for m, v in sorted(buckets.items())]


def by_listing(start=None, end=None, limit=8):
    settings = get_settings()
    pct, listing_fee, _ = _fee_rates(settings)
    conn = get_db()
    rows = _rows(conn, start, end)
    conn.close()
    agg = {}
    for r in rows:
        key = r["listing_id"] or r["item_name"] or "?"
        rev, c, fee, _ = _item_calc(r, pct, listing_fee)
        a = agg.setdefault(key, {"name": r["item_name"] or "(untitled)",
                                 "revenue": 0, "profit": 0, "units": 0})
        a["revenue"] += rev; a["profit"] += rev - c - fee
        a["units"] += r["quantity"] or 0
    out = [{"name": v["name"], "revenue": round(v["revenue"], 2),
            "profit": round(v["profit"], 2), "units": v["units"]}
           for v in agg.values()]
    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out[:limit]


def by_sku(start=None, end=None, limit=12):
    settings = get_settings()
    pct, listing_fee, _ = _fee_rates(settings)
    conn = get_db()
    rows = _rows(conn, start, end)
    conn.close()
    agg = {}
    for r in rows:
        sku = r["sku"] or "(no SKU)"
        rev, c, fee, mapped = _item_calc(r, pct, listing_fee)
        a = agg.setdefault(sku, {"sku": sku, "revenue": 0, "profit": 0,
                                 "units": 0, "mapped": mapped or sku == "(no SKU)"})
        a["revenue"] += rev; a["profit"] += rev - c - fee
        a["units"] += r["quantity"] or 0
        a["mapped"] = a["mapped"] and (mapped or sku == "(no SKU)")
    out = list(agg.values())
    for a in out:
        a["revenue"] = round(a["revenue"], 2)
        a["profit"] = round(a["profit"], 2)
    out.sort(key=lambda x: x["revenue"], reverse=True)
    return out[:limit]


def recent_orders(limit=100):
    conn = get_db()
    rows = conn.execute("""
        SELECT o.order_id, o.channel, o.sale_date, o.buyer, o.order_total,
               COUNT(i.transaction_id) AS lines,
               COALESCE(SUM(i.quantity),0) AS units
        FROM etsy_orders o
        LEFT JOIN etsy_items i ON i.order_id = o.order_id
        GROUP BY o.order_id
        ORDER BY o.sale_date DESC, o.order_id DESC
        LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
