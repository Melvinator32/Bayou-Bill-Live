"""
Etsy + Printify profit tracker — local Flask app.

Run it:
    python app.py
Then open http://127.0.0.1:2298 in your browser.

Everything is stored locally in tracker.db. No external services, no API keys.
"""
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for, flash)

import analytics
from database import (init_db, get_db, get_settings, save_settings)
from importer import import_etsy_csv, import_costs_csv
from connectors import registry
from etsy_auth import etsy_bp
from listing_health import health_bp
from erank import erank_bp

app = Flask(__name__)
app.secret_key = "local-dev-key-not-secret"  # fine: app runs only on your machine

init_db()

# In-app Etsy connect pages (/etsy/setup, /etsy/connect, /etsy/callback)
app.register_blueprint(etsy_bp)

# Listing Health report (/listings)
app.register_blueprint(health_bp)

# eRank keyword bridge (/erank)
app.register_blueprint(erank_bp)


@app.context_processor
def _inject_nav():
    import time as _t
    amap = {"dashboard": "dashboard", "orders": "orders", "costs": "costs",
            "connectors_page": "connectors", "settings": "settings",
            "import_data": "import",
            "listing_health.listings_page": "listings",
            "erank.erank_page": "erank"}
    return {"build": int(_t.time()), "active": amap.get(request.endpoint or "", "")}


@app.route("/")
def dashboard():
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    mn, mx = analytics.date_bounds()

    s = analytics.summary(start, end)
    series = analytics.monthly_series(start, end)
    listings = analytics.by_listing(start, end, limit=8)
    skus = analytics.by_sku(start, end, limit=12)

    return render_template(
        "dashboard.html",
        s=s, series=series, listings=listings, skus=skus,
        start=start or "", end=end or "", min_date=mn, max_date=mx,
        currency=get_settings().get("currency", "USD"),
    )


@app.route("/orders")
def orders():
    return render_template("orders.html", orders=analytics.recent_orders(100))


@app.route("/import", methods=["GET", "POST"])
def import_data():
    if request.method == "POST":
        f = request.files.get("file")
        kind = request.form.get("kind", "etsy")
        if not f or f.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("import_data"))
        data = f.read()
        try:
            if kind == "costs":
                res = import_costs_csv(data, f.filename)
                flash(f"Cost file imported: {res['rows_new']} new, "
                      f"{res['rows_updated']} updated SKUs.", "ok")
            else:
                res = import_etsy_csv(data, f.filename)
                flash(f"Etsy import done: {res['rows_seen']} rows read, "
                      f"{res['rows_new']} new line items, "
                      f"{res['rows_updated']} updated.", "ok")
        except Exception as e:  # surface parse problems instead of 500-ing
            flash(f"Import failed: {e}", "error")
        return redirect(url_for("import_data"))

    conn = get_db()
    log = conn.execute(
        "SELECT * FROM import_log ORDER BY id DESC LIMIT 15"
    ).fetchall()
    conn.close()
    return render_template("import.html", log=log)


@app.route("/costs", methods=["GET", "POST"])
def costs():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        now = datetime.now().isoformat(timespec="seconds")
        if action == "save":
            sku = request.form.get("sku", "").strip()
            listing_id = request.form.get("listing_id", "").strip()
            unit = float(request.form.get("unit_cost") or 0)
            ship = float(request.form.get("ship_cost") or 0)
            name = request.form.get("product_name", "")
            if sku:
                conn.execute(
                    """INSERT INTO sku_costs
                       (sku, product_name, unit_cost, ship_cost, notes, source, updated_at)
                       VALUES (?,?,?,?,'','manual',?)
                       ON CONFLICT(sku) DO UPDATE SET
                         product_name=excluded.product_name, unit_cost=excluded.unit_cost,
                         ship_cost=excluded.ship_cost, source='manual',
                         updated_at=excluded.updated_at""",
                    (sku, name, unit, ship, now))
                flash(f"Saved cost for SKU {sku}.", "ok")
            elif listing_id:
                conn.execute(
                    """INSERT INTO listing_costs
                       (listing_id, product_name, unit_cost, ship_cost, source, updated_at)
                       VALUES (?,?,?,?,'manual',?)
                       ON CONFLICT(listing_id) DO UPDATE SET
                         product_name=excluded.product_name, unit_cost=excluded.unit_cost,
                         ship_cost=excluded.ship_cost, source='manual',
                         updated_at=excluded.updated_at""",
                    (listing_id, name, unit, ship, now))
                flash("Saved cost for that listing.", "ok")
        conn.commit()
        conn.close()
        return redirect(url_for("costs"))

    # One row per sold product (grouped by listing), with its matched cost.
    # A SKU-keyed cost wins if present; otherwise a listing-keyed (Printify) cost.
    seen = conn.execute("""
        SELECT i.listing_id AS listing_id, i.sku AS sku,
               MAX(i.item_name) AS name, SUM(i.quantity) AS units,
               sc.unit_cost AS sc_unit, sc.ship_cost AS sc_ship, sc.source AS sc_source,
               lc.unit_cost AS lc_unit, lc.ship_cost AS lc_ship, lc.source AS lc_source
        FROM etsy_items i
        LEFT JOIN sku_costs sc ON sc.sku = i.sku AND i.sku != ''
        LEFT JOIN listing_costs lc ON lc.listing_id != ''
              AND (lc.listing_id = i.listing_id OR lc.listing_id = i.sku)
        GROUP BY i.listing_id, i.sku
        ORDER BY units DESC
    """).fetchall()
    conn.close()

    rows = []
    for r in seen:
        if r["sc_unit"] is not None:
            unit, ship, source, key_kind = r["sc_unit"], r["sc_ship"], r["sc_source"] or "manual", "sku"
        elif r["lc_unit"] is not None:
            unit, ship, source, key_kind = r["lc_unit"], r["lc_ship"], r["lc_source"] or "printify", "listing"
        else:
            unit, ship, source, key_kind = None, None, None, ("sku" if r["sku"] else "listing")
        rows.append({
            "name": r["name"] or "(untitled)", "sku": r["sku"] or "",
            "listing_id": r["listing_id"] or "", "units": r["units"],
            "unit_cost": unit, "ship_cost": ship, "source": source,
            "mapped": unit is not None, "key_kind": key_kind,
        })
    rows.sort(key=lambda x: (x["mapped"], x["units"]), reverse=True)
    return render_template("costs.html", rows=rows)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        save_settings({
            "transaction_fee_pct": request.form.get("transaction_fee_pct", "6.5"),
            "processing_fee_pct": request.form.get("processing_fee_pct", "3.0"),
            "processing_fee_fixed": request.form.get("processing_fee_fixed", "0.25"),
            "listing_fee": request.form.get("listing_fee", "0.20"),
            "offsite_ads_pct": request.form.get("offsite_ads_pct", "0.0"),
            "currency": request.form.get("currency", "USD"),
        })
        flash("Settings saved.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", s=get_settings())


@app.route("/connectors", methods=["GET", "POST"])
def connectors_page():
    conns = registry.discover()
    if request.method == "POST":
        key = request.form.get("connector")
        action = request.form.get("action")
        c = conns.get(key)
        if not c:
            flash("Unknown connector.", "error")
            return redirect(url_for("connectors_page"))

        if action == "save":
            # Only write non-blank fields, so leaving a password blank keeps
            # the stored value instead of wiping it.
            updates = {f.key: request.form.get(f.key, "")
                       for f in c.settings_fields
                       if request.form.get(f.key, "").strip() != ""}
            if updates:
                save_settings(updates)
            flash(f"{c.label} settings saved.", "ok")
        elif action == "test":
            res = c.test(get_settings())
            flash(f"{c.label}: {res['message']}", "ok" if res["ok"] else "error")
        elif action == "sync":
            try:
                r = c.sync(get_settings())
                flash(f"{c.label} sync done: {r['rows_new']} new, "
                      f"{r['rows_updated']} updated ({r['rows_seen']} seen).", "ok")
            except Exception as e:
                flash(f"{c.label} sync failed: {e}", "error")
        return redirect(url_for("connectors_page"))

    settings = get_settings()
    cards = []
    for c in conns.values():
        fields = [{"key": f.key, "label": f.label, "kind": f.kind,
                   "help": f.help, "set": bool(settings.get(f.key))}
                  for f in c.settings_fields]
        cards.append({"key": c.key, "label": c.label, "kind": c.kind,
                      "docs_url": c.docs_url, "implemented": c.implemented,
                      "configured": c.is_configured(settings), "fields": fields})
    return render_template("connectors.html", cards=cards)


@app.template_filter("money")
def money(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return v


if __name__ == "__main__":
    # Port 2298 ("BAYU" on a keypad) keeps this app off the common dev ports
    # (5000/3000/8000/8080) so it never clashes with other local projects.
    # use_reloader=False -> single process (cleaner with the SQLite file).
    app.run(debug=True, use_reloader=False, port=2298)
