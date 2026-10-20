"""
Listing Health — finds the highest-ROI fixes across your ACTIVE Etsy listings.

Pulls your live listings from the Etsy Open API (tags, title, price, materials,
quantity, favorites), joins them to the Printify costs already in tracker.db via
the same dual bridge the Costs page uses (variant SKU first, listing id second),
scores a short list of cheap-to-fix problems, and ranks them by how much money
the listing actually makes -- so a missing tag on a proven seller outranks the
same gap on a listing that's never sold.

Adds two pages:
  /listings          the report
  /listings/sync     POST: refresh from Etsy, then redirect back

Wire it up in app.py with two lines:
    from listing_health import health_bp
    app.register_blueprint(health_bp)

Auth: tries your stored OAuth token first, then falls back to keystring-only
(getListingsByShop accepts either). If your token predates this module it won't
carry the listings_r scope -- the fallback usually still works; if it doesn't,
reconnect at /etsy/setup (SCOPES now includes listings_r).

Margin basis: GROSS margin against Printify PRODUCTION cost only (unit_cost),
matching your "price = cost / 0.60, shipping charged separately" rule. Printify
shipping is deliberately excluded because your buyers pay shipping. Etsy fees
are shown as an FYI net figure but do NOT drive the flag.
"""
import math
import re
import time
from datetime import datetime

import requests
from flask import (Blueprint, request, redirect, url_for, flash,
                   render_template_string)

from connectors.etsy import EtsyConnector, _money
from database import get_db, get_settings, save_settings

try:                                   # optional: drop in erank.py to enable
    import erank
except ImportError:                    # pragma: no cover
    erank = None

health_bp = Blueprint("listing_health", __name__)

API_BASE = "https://openapi.etsy.com/v3/application"

MAX_TAGS = 13          # Etsy hard limit
MAX_TITLE = 140        # Etsy hard limit
TITLE_TARGET = 100     # below this, you're leaving keyword room unused
DESC_TARGET = 160      # first ~160 chars carry the SEO weight
DEFAULT_TARGET_MARGIN = 40.0

LISTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS etsy_listings (
    listing_id      TEXT PRIMARY KEY,
    title           TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    state           TEXT DEFAULT '',
    price           REAL DEFAULT 0,
    currency        TEXT DEFAULT 'USD',
    quantity        INTEGER DEFAULT 0,
    tags            TEXT DEFAULT '',
    materials       TEXT DEFAULT '',
    taxonomy_id     TEXT DEFAULT '',
    shop_section_id TEXT DEFAULT '',
    num_favorers    INTEGER DEFAULT 0,
    url             TEXT DEFAULT '',
    synced_at       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_listings_state ON etsy_listings(state);
"""


def _ensure_table():
    conn = get_db()
    try:
        conn.executescript(LISTINGS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Etsy fetch
# --------------------------------------------------------------------------
def _get_public(path, settings):
    """Keystring-only GET, for when the OAuth token lacks listings_r."""
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"x-api-key": f"{settings.get('etsy_keystring','')}:"
                              f"{settings.get('etsy_shared_secret','')}"},
        timeout=(10, 60))
    resp.raise_for_status()
    return resp.json()


def fetch_active_listings(settings):
    """Page through active listings. Returns a list of dicts ready for upsert."""
    shop_id = settings.get("etsy_shop_id")
    if not (shop_id and settings.get("etsy_keystring")
            and settings.get("etsy_shared_secret")):
        raise RuntimeError("Etsy isn't configured — connect it at /etsy/setup first.")

    ec = EtsyConnector()
    try:
        token = ec._access_token(settings)
    except Exception:
        token = None   # fall straight to keystring-only

    out, offset, limit = [], 0, 100
    while True:
        path = (f"/shops/{shop_id}/listings"
                f"?state=active&limit={limit}&offset={offset}")
        if token:
            try:
                data = ec._get(path, settings, token)
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code in (401, 403):
                    token = None                       # scope missing; downgrade
                    data = _get_public(path, settings)
                else:
                    raise
        else:
            data = _get_public(path, settings)

        results = data.get("results", []) if isinstance(data, dict) else []
        for r in results:
            out.append({
                "listing_id": str(r.get("listing_id") or ""),
                "title": r.get("title") or "",
                "description": r.get("description") or "",
                "state": r.get("state") or "",
                "price": _money(r.get("price")),
                "currency": (r.get("price") or {}).get("currency_code", "USD"),
                "quantity": int(r.get("quantity") or 0),
                "tags": ", ".join(r.get("tags") or []),
                "materials": ", ".join(r.get("materials") or []),
                "taxonomy_id": str(r.get("taxonomy_id") or ""),
                "shop_section_id": str(r.get("shop_section_id") or ""),
                "num_favorers": int(r.get("num_favorers") or 0),
                "url": r.get("url") or "",
            })
        count = data.get("count", 0) if isinstance(data, dict) else 0
        offset += limit
        if not results or offset >= count:
            break
        time.sleep(0.2)
    return out


def sync_listings(settings):
    """Fetch + upsert. Returns {'rows_seen','rows_new','rows_updated'}."""
    _ensure_table()
    rows = fetch_active_listings(settings)
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    new = updated = 0
    try:
        for r in rows:
            if not r["listing_id"]:
                continue
            exists = conn.execute("SELECT 1 FROM etsy_listings WHERE listing_id=?",
                                  (r["listing_id"],)).fetchone()
            conn.execute("""
                INSERT INTO etsy_listings
                    (listing_id, title, description, state, price, currency,
                     quantity, tags, materials, taxonomy_id, shop_section_id,
                     num_favorers, url, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    title=excluded.title, description=excluded.description,
                    state=excluded.state, price=excluded.price,
                    currency=excluded.currency, quantity=excluded.quantity,
                    tags=excluded.tags, materials=excluded.materials,
                    taxonomy_id=excluded.taxonomy_id,
                    shop_section_id=excluded.shop_section_id,
                    num_favorers=excluded.num_favorers, url=excluded.url,
                    synced_at=excluded.synced_at""",
                (r["listing_id"], r["title"], r["description"], r["state"],
                 r["price"], r["currency"], r["quantity"], r["tags"],
                 r["materials"], r["taxonomy_id"], r["shop_section_id"],
                 r["num_favorers"], r["url"], now))
            new += 0 if exists else 1
            updated += 1 if exists else 0
        conn.execute("""INSERT INTO import_log
            (source, filename, rows_seen, rows_new, rows_updated, imported_at)
            VALUES ('etsy-listings','API sync (active listings)',?,?,?,?)""",
            (len(rows), new, updated, now))
        conn.commit()
    finally:
        conn.close()
    return {"rows_seen": len(rows), "rows_new": new, "rows_updated": updated}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _to_99(p):
    """Round UP to the next .99 price point."""
    base = math.floor(p)
    cand = base + 0.99
    if cand < p - 1e-9:
        cand = base + 1.99
    return round(cand, 2)


def _norm_tag(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _sales_and_costs():
    """Per-listing revenue/units, and unit cost via the SKU->listing dual bridge."""
    conn = get_db()
    try:
        sales = {r["lid"]: {"revenue": r["revenue"] or 0, "units": r["units"] or 0}
                 for r in conn.execute("""
                     SELECT listing_id AS lid, SUM(item_total) AS revenue,
                            SUM(quantity) AS units
                     FROM etsy_items WHERE listing_id != ''
                     GROUP BY listing_id""")}
        # Bridge 1: SKUs sold under this listing that have a Printify SKU cost.
        sku_bridge = {r["lid"]: r["unit"] for r in conn.execute("""
            SELECT i.listing_id AS lid, AVG(sc.unit_cost) AS unit
            FROM etsy_items i
            JOIN sku_costs sc ON sc.sku = i.sku AND i.sku != ''
            WHERE i.listing_id != '' AND sc.unit_cost > 0
            GROUP BY i.listing_id""")}
        # Bridge 2: Printify product -> Etsy listing id.
        lst_bridge = {r["listing_id"]: r["unit_cost"] for r in conn.execute(
            "SELECT listing_id, unit_cost FROM listing_costs WHERE unit_cost > 0")}
    finally:
        conn.close()
    return sales, sku_bridge, lst_bridge


def _fee_pct(settings):
    g = lambda k, d: float(settings.get(k) or d)
    return (g("transaction_fee_pct", 6.5) + g("processing_fee_pct", 3.0)
            + g("offsite_ads_pct", 0.0)) / 100.0


def build_report(settings):
    _ensure_table()
    target = float(settings.get("target_margin_pct") or DEFAULT_TARGET_MARGIN)
    fee_pct = _fee_pct(settings)
    listing_fee = float(settings.get("listing_fee") or 0.20)

    conn = get_db()
    try:
        listings = [dict(r) for r in conn.execute(
            "SELECT * FROM etsy_listings ORDER BY listing_id")]
        synced_at = conn.execute(
            "SELECT MAX(synced_at) m FROM etsy_listings").fetchone()["m"] or ""
    finally:
        conn.close()

    sales, sku_bridge, lst_bridge = _sales_and_costs()
    # Loaded once, reused for every listing (it's the whole keyword table).
    kw_pool = erank._all_keywords() if erank else []
    rows = []

    for L in listings:
        lid = L["listing_id"]
        s = sales.get(lid, {"revenue": 0, "units": 0})
        revenue, units = round(s["revenue"], 2), s["units"]
        price = L["price"] or 0
        cost = sku_bridge.get(lid) or lst_bridge.get(lid)

        tags = _split(L["tags"])
        materials = _split(L["materials"])
        title = L["title"] or ""
        desc = L["description"] or ""
        favs = L["num_favorers"] or 0

        issues = []

        def add(code, sev, points, label, fix):
            issues.append({"code": code, "sev": sev, "points": round(points, 1),
                           "label": label, "fix": fix})

        # 1. Active but nothing to sell.
        if (L["quantity"] or 0) <= 0:
            add("quantity_zero", "high", 15,
                "Out of stock",
                "Quantity is 0 on an active listing — set it back to your normal "
                "stock number.")

        # 2. Margin against the Printify production cost.
        gross = None
        if cost and price > 0:
            gross = (price - cost) / price * 100.0
            if gross < target - 0.5:
                short = target - gross
                denom = 1 - (target / 100.0)
                want = _to_99(cost / denom) if denom > 0 else None
                pts = min(25.0, max(6.0, short * 0.8))
                add("margin_low", "high", pts,
                    f"Margin {gross:.0f}% (target {target:.0f}%)",
                    (f"Raise price from ${price:.2f} to ${want:.2f} "
                     f"(cost ${cost:.2f} ÷ {denom:.2f})."
                     if want else f"Cost ${cost:.2f} can't reach a {target:.0f}% "
                                  f"margin — target is 100% or more."))
        elif not cost:
            add("no_cost", "low", 2,
                "No Printify cost mapped",
                "Margin can't be checked. Run the Printify sync, or add a cost "
                "on the Costs page.")

        # 3. Unused tag slots.
        if len(tags) < MAX_TAGS:
            missing = MAX_TAGS - len(tags)
            picks = (erank.suggest_tags(title, tags, n=missing, pool=kw_pool)
                     if kw_pool else [])
            if picks:
                fix = (f"Add {missing}. Best unused eRank keywords for this "
                       f"listing: {', '.join(picks)}")
            else:
                fix = (f"Add {missing} more tag{'s' if missing != 1 else ''} — every "
                       f"empty slot is a search phrase you can't rank for."
                       + ("" if erank is None else
                          " Import an eRank export at /erank to get named picks."))
            add("tags_low", "high" if missing >= 4 else "med", 1.5 * missing,
                f"{len(tags)}/{MAX_TAGS} tags", fix)

        # 4. Duplicate tags waste those slots.
        seen, dupes = set(), []
        for t in tags:
            n = _norm_tag(t)
            if n and n in seen:
                dupes.append(t)
            seen.add(n)
        if dupes:
            add("tags_dupe", "med", 3.0 * len(dupes),
                f"{len(dupes)} duplicate tag{'s' if len(dupes) != 1 else ''}",
                f"Replace: {', '.join(dupes[:4])}")

        # 5. Unused title characters.
        if len(title) < TITLE_TARGET:
            add("title_short", "med", (MAX_TITLE - len(title)) * 0.12,
                f"Title {len(title)}/{MAX_TITLE} chars",
                f"{MAX_TITLE - len(title)} characters unused — extend with "
                f"buyer-phrase keywords, not repeats.")

        # 6. Favorited but never sold: a price or photo problem, not a traffic one.
        if favs >= 5 and units == 0:
            add("favs_no_sales", "high", 8,
                f"{favs} faves, 0 sales",
                "People find it and like it but don't buy — test the price or "
                "the first photo.")

        # 7. Materials is a free ranking field.
        if not materials:
            add("materials_missing", "low", 4,
                "No materials",
                "Add materials (e.g. vinyl, laminate, waterproof ink).")

        # 8. Description opener.
        if len(desc) < DESC_TARGET:
            add("desc_short", "low", 3,
                f"Description {len(desc)} chars",
                "First ~160 characters carry the SEO weight — write a real opener.")

        if not issues:
            continue

        points = sum(i["points"] for i in issues)
        weight = 1.0 + min(revenue, 500.0) / 100.0     # 1.0 .. 6.0
        net = None
        if cost and price > 0:
            net = (price * (1 - fee_pct) - listing_fee - cost) / price * 100.0

        rows.append({
            "listing_id": lid, "title": title, "url": L["url"],
            "price": price, "cost": cost, "gross": gross, "net": net,
            "revenue": revenue, "units": units, "favs": favs,
            "tag_count": len(tags), "issues": issues,
            "points": round(points, 1),
            "priority": round(points * weight, 1),
        })

    rows.sort(key=lambda r: r["priority"], reverse=True)
    counts = {}
    for r in rows:
        for i in r["issues"]:
            counts[i["code"]] = counts.get(i["code"], 0) + 1
    return {
        "rows": rows, "counts": counts, "synced_at": synced_at,
        "total_listings": len(listings), "flagged": len(rows),
        "target": target,
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
PAGE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listing Health</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--sage:#77b28c;--teal:#00859b;--cyan:#00bfb8;--green:#38a66f;
        --lime:#bce194;--paleteal:#cce7eb;--offwhite:#ebf2e6;--cream:#f8ecdb;
        --lgray:#d9d9d6;--mgray:#b3b3b3;}
  *{box-sizing:border-box}
  body{font-family:'Outfit',system-ui,sans-serif;background:var(--offwhite);
       color:#15302b;margin:0;padding:2rem 1rem;}
  .wrap{max-width:1100px;margin:0 auto;}
  h1{font-family:'Fraunces',serif;color:var(--teal);margin:0 0 .25rem;}
  .sub{color:#4b665e;margin:0 0 1.25rem;font-size:.95rem;}
  .card{background:#fff;border:1px solid var(--lgray);border-radius:14px;
        padding:1.25rem;margin-bottom:1.25rem;box-shadow:0 1px 0 rgba(0,0,0,.03);}
  .bar{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;}
  .stat{background:var(--paleteal);border-radius:10px;padding:.6rem .9rem;
        min-width:120px;}
  .stat b{display:block;font-family:'Fraunces',serif;font-size:1.35rem;
          color:var(--teal);line-height:1.1;}
  .stat span{font-size:.78rem;color:#4b665e;}
  button,.btn{display:inline-block;border:none;border-radius:10px;
        padding:.6rem 1.1rem;font-family:inherit;font-weight:600;font-size:.92rem;
        cursor:pointer;text-decoration:none;}
  .primary{background:var(--teal);color:#fff;}
  .ghost{background:var(--cream);color:#7a5a23;}
  .chipbar{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.9rem;}
  .chip{display:inline-block;padding:.28rem .65rem;border-radius:999px;
        font-size:.78rem;font-weight:600;text-decoration:none;
        background:var(--lgray);color:#3a4a45;}
  .chip.on{background:var(--teal);color:#fff;}
  table{width:100%;border-collapse:collapse;font-size:.88rem;}
  th{text-align:left;font-size:.74rem;text-transform:uppercase;letter-spacing:.05em;
     color:#4b665e;border-bottom:2px solid var(--lgray);padding:.5rem .5rem;}
  td{border-bottom:1px solid var(--offwhite);padding:.7rem .5rem;vertical-align:top;}
  tr:hover td{background:#fbfdf9;}
  .ttl{font-weight:600;max-width:340px;}
  .ttl a{color:#15302b;text-decoration:none;}
  .ttl a:hover{color:var(--teal);text-decoration:underline;}
  .meta{color:#7b8b86;font-size:.76rem;font-weight:400;margin-top:.15rem;}
  .pri{font-family:'Fraunces',serif;font-size:1.1rem;color:var(--teal);}
  .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}
  .iss{margin:0 0 .45rem;}
  .iss:last-child{margin-bottom:0;}
  .sev{display:inline-block;padding:.18rem .5rem;border-radius:6px;
       font-size:.74rem;font-weight:600;margin-right:.4rem;}
  .sev.high{background:#f7dcdc;color:#7a2222;}
  .sev.med{background:var(--cream);color:#7a5a23;}
  .sev.low{background:var(--paleteal);color:#0a4a52;}
  .fix{color:#4b665e;font-size:.82rem;}
  .flash{padding:.7rem .9rem;border-radius:9px;margin-bottom:.8rem;font-weight:500;}
  .flash.ok{background:var(--paleteal);color:#0a4a52;}
  .flash.error{background:#f7dcdc;color:#7a2222;}
  .links{margin-top:1rem;font-size:.9rem;} .links a{color:var(--teal);}
  input[type=number]{width:70px;padding:.4rem;border:1px solid var(--mgray);
        border-radius:8px;font-family:inherit;}
  .empty{padding:2rem;text-align:center;color:#4b665e;}
</style></head><body><div class="wrap">
  <h1>Listing Health</h1>
  <p class="sub">Every active listing, scored on the fixes that pay for themselves
     fastest. Priority = issue weight &times; how much that listing has actually
     earned, so proven sellers float to the top.</p>

  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat,msg in msgs %}<div class="flash {{cat}}">{{msg}}</div>{% endfor %}
  {% endwith %}

  <div class="card">
    <div class="bar">
      <div class="stat"><b>{{ r.total_listings }}</b><span>active listings</span></div>
      <div class="stat"><b>{{ r.flagged }}</b><span>with fixes</span></div>
      <div class="stat"><b>{{ r.counts.get('tags_low', 0) }}</b><span>under 13 tags</span></div>
      <div class="stat"><b>{{ r.counts.get('margin_low', 0) }}</b><span>under target margin</span></div>
      <div style="flex:1"></div>
      <form method="post" action="{{ url_for('listing_health.sync_page') }}"
            style="display:inline">
        <button class="primary" type="submit">Sync from Etsy</button>
      </form>
      <form method="post" action="{{ url_for('listing_health.set_target') }}"
            style="display:flex;gap:.4rem;align-items:center">
        <label style="font-size:.85rem;color:#4b665e">Target margin %</label>
        <input type="number" name="target_margin_pct" step="0.5" min="0" max="95"
               value="{{ r.target }}">
        <button class="ghost" type="submit">Set</button>
      </form>
    </div>
    <div class="chipbar">
      <a class="chip {{ 'on' if not flt }}" href="{{ url_for('listing_health.listings_page') }}">All</a>
      {% for code, label in filters %}
        {% if r.counts.get(code) %}
        <a class="chip {{ 'on' if flt == code }}"
           href="{{ url_for('listing_health.listings_page', issue=code) }}">
           {{ label }} ({{ r.counts.get(code) }})</a>
        {% endif %}
      {% endfor %}
    </div>
    {% if r.synced_at %}
      <p class="meta" style="margin-top:.8rem">Last synced {{ r.synced_at }}
         &middot; margin measured against Printify production cost only
         (you charge shipping separately).</p>
    {% endif %}
  </div>

  <div class="card">
  {% if not rows %}
    <div class="empty">
      {% if r.total_listings == 0 %}
        No listings pulled yet — hit <b>Sync from Etsy</b> above.
      {% else %}
        Nothing flagged{{ ' for this filter' if flt else '' }}. Clean shop.
      {% endif %}
    </div>
  {% else %}
    <table>
      <thead><tr>
        <th class="num">Pri</th><th>Listing</th>
        <th class="num">Price</th><th class="num">Cost</th><th class="num">Margin</th>
        <th class="num">Sold</th><th>What to fix</th>
      </tr></thead>
      <tbody>
      {% for row in rows %}
        <tr>
          <td class="num"><span class="pri">{{ row.priority }}</span></td>
          <td class="ttl">
            {% if row.url %}<a href="{{ row.url }}" target="_blank">{{ row.title[:78] }}</a>
            {% else %}{{ row.title[:78] }}{% endif %}
            <div class="meta">{{ row.tag_count }} tags &middot; {{ row.favs }} faves
              &middot; id {{ row.listing_id }}</div>
          </td>
          <td class="num">${{ '%.2f'|format(row.price) }}</td>
          <td class="num">{{ '$%.2f'|format(row.cost) if row.cost else '—' }}</td>
          <td class="num">
            {% if row.gross is not none %}{{ '%.0f'|format(row.gross) }}%
              <div class="meta">net {{ '%.0f'|format(row.net) }}%</div>
            {% else %}—{% endif %}
          </td>
          <td class="num">{{ row.units }}
            <div class="meta">${{ '%.0f'|format(row.revenue) }}</div>
          </td>
          <td>
            {% for i in row.issues %}
              <div class="iss">
                <span class="sev {{ i.sev }}">{{ i.label }}</span>
                <span class="fix">{{ i.fix }}</span>
              </div>
            {% endfor %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% endif %}
  </div>

  <div class="links">
    <a href="/">← Dashboard</a> &nbsp;·&nbsp;
    <a href="/costs">Costs</a> &nbsp;·&nbsp;
    <a href="/connectors">Connectors</a>
  </div>
</div></body></html>
"""

FILTERS = [
    ("quantity_zero", "Out of stock"),
    ("margin_low", "Under margin"),
    ("tags_low", "Missing tags"),
    ("favs_no_sales", "Faves, no sales"),
    ("tags_dupe", "Duplicate tags"),
    ("title_short", "Short title"),
    ("materials_missing", "No materials"),
    ("desc_short", "Short description"),
    ("no_cost", "No cost mapped"),
]


@health_bp.route("/listings")
def listings_page():
    settings = get_settings()
    r = build_report(settings)
    flt = request.args.get("issue") or ""
    rows = r["rows"]
    if flt:
        rows = [x for x in rows if any(i["code"] == flt for i in x["issues"])]
    return render_template_string(PAGE_HTML, r=r, rows=rows, flt=flt,
                                  filters=FILTERS)


@health_bp.route("/listings/sync", methods=["POST"])
def sync_page():
    try:
        res = sync_listings(get_settings())
        flash(f"Pulled {res['rows_seen']} active listings "
              f"({res['rows_new']} new, {res['rows_updated']} updated).", "ok")
    except Exception as e:
        flash(f"Listing sync failed: {e}", "error")
    return redirect(url_for("listing_health.listings_page"))


@health_bp.route("/listings/target", methods=["POST"])
def set_target():
    v = (request.form.get("target_margin_pct") or "").strip()
    try:
        float(v)
        save_settings({"target_margin_pct": v})
        flash(f"Target margin set to {v}%.", "ok")
    except ValueError:
        flash("Target margin must be a number.", "error")
    return redirect(url_for("listing_health.listings_page"))
