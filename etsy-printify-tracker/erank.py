"""
eRank bridge — imports keyword exports from eRank and turns them into concrete
tag suggestions for your listings.

eRank has NO public API (web app + Chrome extension only), and scraping it would
need your session cookie and break their terms. CSV export is the supported path,
so that's what this does: you download from eRank, drop the file here, and the
Listing Health page stops saying "add 6 more tags" and starts saying WHICH tags.

Adds one page:
  /erank              upload exports, see unused keyword opportunities
  /erank/import       POST: parse a CSV
  /erank/clear        POST: wipe the keyword table

Wire it up in app.py with two lines:
    from erank import erank_bp
    app.register_blueprint(erank_bp)

Where to get the CSVs in eRank (any of these work — headers are auto-detected):
  Tools > Keyword Tool        -> search a seed term, download results
  Tools > Keyword Lists       -> export a saved list
  Tools > Listing Audit       -> export (keyword columns are picked up, rest ignored)
  Tools > Top Sellers / Trend Buzz -> export

Column matching is alias-based and forgiving (same approach as importer.py). If a
file imports with 0 rows, the page prints the headers it actually saw so the alias
list below can be extended in one line.

Relative values ("High"/"Medium"/"Low") are mapped to representative numbers so
they still rank; exact numbers are always preferred when eRank supplies them.
"""
import csv
import io
import re
from datetime import datetime

from flask import (Blueprint, request, redirect, url_for, flash,
                   render_template_string)

from database import get_db
from importer import _norm

erank_bp = Blueprint("erank", __name__)

ETSY_TAG_MAX_CHARS = 20        # Etsy hard limit per tag

SCHEMA = """
CREATE TABLE IF NOT EXISTS erank_keywords (
    keyword         TEXT PRIMARY KEY,
    display         TEXT DEFAULT '',
    avg_searches    REAL DEFAULT 0,
    avg_clicks      REAL DEFAULT 0,
    competition     REAL DEFAULT 0,
    ctr             REAL DEFAULT 0,
    google_searches REAL DEFAULT 0,
    opportunity     REAL DEFAULT 0,
    source_file     TEXT DEFAULT '',
    imported_at     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_erank_opp ON erank_keywords(opportunity DESC);
"""

# Header aliases, normalized by importer._norm (lowercase, spaces -> underscores).
ALIASES = {
    "keyword": ("keyword", "keywords", "long_tail_keyword", "long_tail_keywords",
                "search_term", "search_terms", "term", "tag", "tags", "phrase",
                "top_keyword", "suggested_keyword"),
    "avg_searches": ("average_searches", "avg_searches", "etsy_avg_searches",
                     "etsy_average_searches", "searches", "monthly_searches",
                     "etsy_searches", "search_volume", "volume",
                     "avg_searches_etsy"),
    "avg_clicks": ("average_clicks", "avg_clicks", "etsy_avg_clicks",
                   "etsy_average_clicks", "clicks", "monthly_clicks"),
    "competition": ("etsy_competition", "competition", "competitors",
                    "competing_listings", "results", "listings", "etsy_results",
                    "number_of_listings"),
    "ctr": ("etsy_click_through_rate", "click_through_rate", "ctr",
            "etsy_ctr", "click_thru_rate"),
    "google_searches": ("google_searches", "google_avg_searches",
                        "google_average_searches", "google_volume",
                        "google_monthly_searches"),
}

# eRank sometimes ships relative bands instead of numbers. Scale differs per
# column: "high competition" is a much bigger number than "high searches".
BANDS = {
    "avg_searches": {"very low": 20, "low": 150, "medium": 1200, "med": 1200,
                     "moderate": 1200, "high": 10000, "very high": 50000},
    "avg_clicks": {"very low": 5, "low": 40, "medium": 300, "med": 300,
                   "moderate": 300, "high": 2500, "very high": 12000},
    "competition": {"very low": 200, "low": 800, "medium": 6000, "med": 6000,
                    "moderate": 6000, "high": 50000, "very high": 200000},
}

STOP = {"the", "and", "for", "with", "your", "you", "from", "this", "that",
        "are", "was", "our", "all", "any", "can", "has", "have", "not", "but",
        "inspired", "gift", "gifts", "new", "set", "pack", "gray", "grey"}


def _ensure_table():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def _read_table(data, filename="erank.csv"):
    """Yield rows as normalized dicts, plus the delimiter that was detected.

    Sniffs comma / tab / semicolon / pipe so a table selected in eRank's UI and
    pasted into a .txt (tab-separated) imports just as well as a real CSV export.
    """
    text = (data.decode("utf-8-sig", errors="replace")
            if isinstance(data, bytes) else data).lstrip("\r\n")
    lines = text.splitlines()
    head = lines[0] if lines else ""
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
    except csv.Error:
        delim = max(",\t;|", key=head.count)
        if head.count(delim) == 0:
            delim = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = [{_norm(k): v for k, v in raw.items() if k is not None}
            for raw in reader]
    return rows, delim


def _pick_key(row, field):
    """Return the raw value for a logical field, or '' if no alias matched."""
    for a in ALIASES[field]:
        if a in row and str(row[a]).strip() != "":
            return row[a]
    return ""


def _number(v, field=None):
    """Parse '1,234', '12.5%', '1.2K', or a relative band into a float."""
    if v is None:
        return 0.0
    s = str(v).strip().lower()
    if s in ("", "-", "n/a", "na", "unknown"):
        return 0.0
    band = BANDS.get(field or "")
    if band:
        for label, val in sorted(band.items(), key=lambda kv: -len(kv[0])):
            if s == label or s.startswith(label + " "):
                return float(val)
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1000.0, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    s = s.replace(",", "").replace("$", "").replace("%", "").replace("+", "").strip()
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def _norm_kw(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _opportunity(searches, competition):
    """Demand per unit of supply. Higher is better. Unitless, comparable."""
    if searches <= 0:
        return 0.0
    return round(searches / max(competition, 1.0) * 1000.0, 2)


def import_erank_csv(data, filename="erank.csv"):
    """Returns {'rows_seen','rows_new','rows_updated','headers','skipped','delim'}."""
    _ensure_table()
    rows, delim = _read_table(data, filename)
    headers = sorted(rows[0].keys()) if rows else []
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    seen = new = updated = skipped = 0
    try:
        for row in rows:
            seen += 1
            display = str(_pick_key(row, "keyword")).strip()
            key = _norm_kw(display)
            if not key:
                skipped += 1
                continue
            searches = _number(_pick_key(row, "avg_searches"), "avg_searches")
            clicks = _number(_pick_key(row, "avg_clicks"), "avg_clicks")
            comp = _number(_pick_key(row, "competition"), "competition")
            ctr = _number(_pick_key(row, "ctr"))
            goog = _number(_pick_key(row, "google_searches"))
            opp = _opportunity(searches, comp)

            exists = conn.execute(
                "SELECT 1 FROM erank_keywords WHERE keyword=?", (key,)).fetchone()
            # Keep the richer row when the same keyword shows up in two exports.
            conn.execute("""
                INSERT INTO erank_keywords
                    (keyword, display, avg_searches, avg_clicks, competition,
                     ctr, google_searches, opportunity, source_file, imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(keyword) DO UPDATE SET
                    display=excluded.display,
                    avg_searches=MAX(erank_keywords.avg_searches, excluded.avg_searches),
                    avg_clicks=MAX(erank_keywords.avg_clicks, excluded.avg_clicks),
                    competition=MAX(erank_keywords.competition, excluded.competition),
                    ctr=MAX(erank_keywords.ctr, excluded.ctr),
                    google_searches=MAX(erank_keywords.google_searches,
                                        excluded.google_searches),
                    opportunity=MAX(erank_keywords.opportunity, excluded.opportunity),
                    source_file=excluded.source_file,
                    imported_at=excluded.imported_at""",
                (key, display, searches, clicks, comp, ctr, goog, opp,
                 filename, now))
            new += 0 if exists else 1
            updated += 1 if exists else 0
        conn.execute("""INSERT INTO import_log
            (source, filename, rows_seen, rows_new, rows_updated, imported_at)
            VALUES ('erank',?,?,?,?,?)""", (filename, seen, new, updated, now))
        conn.commit()
    finally:
        conn.close()
    return {"rows_seen": seen, "rows_new": new, "rows_updated": updated,
            "headers": headers, "skipped": skipped,
            "delim": {",": "comma", "\t": "tab", ";": "semicolon",
                      "|": "pipe"}.get(delim, delim)}


# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------
def _stem(w):
    """Crude singularizer — 'stickers' and 'sticker' must match or relevance
    scoring misses the obvious. Not linguistics, just plural collapsing."""
    for suf in ("ies", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-3] + "y" if suf == "ies" else w[:-len(suf)]
    return w


def _tokens(s):
    return {_stem(w) for w in re.split(r"[^a-z0-9]+", (s or "").lower())
            if len(w) > 2 and w not in STOP}


def _all_keywords():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM erank_keywords WHERE opportunity > 0 "
            "ORDER BY opportunity DESC")]
        conn.commit()
    finally:
        conn.close()
    return rows


def _tags_in_use():
    """Every normalized tag currently on an active listing."""
    conn = get_db()
    try:
        try:
            rows = conn.execute("SELECT tags FROM etsy_listings").fetchall()
        except Exception:
            return set()
    finally:
        conn.close()
    used = set()
    for r in rows:
        for t in (r["tags"] or "").split(","):
            n = _norm_kw(t)
            if n:
                used.add(n)
    return used


def suggest_tags(title, existing_tags, n=6, pool=None):
    """Top-n eRank keywords that fit Etsy's 20-char tag limit, aren't already on
    this listing, and share vocabulary with the title. Returns display strings."""
    pool = pool if pool is not None else _all_keywords()
    if not pool:
        return []
    have = {_norm_kw(t) for t in (existing_tags or []) if _norm_kw(t)}
    want = _tokens(title)
    scored = []
    for k in pool:
        kw = k["keyword"]
        if kw in have or len(kw) > ETSY_TAG_MAX_CHARS:
            continue
        if any(kw in h or h in kw for h in have):   # near-duplicate of a tag
            continue
        overlap = len(_tokens(kw) & want)
        if overlap == 0:
            continue                                 # off-topic for this listing
        scored.append((k["opportunity"] * (1 + overlap), k["display"] or kw))
    scored.sort(reverse=True)
    return [d for _, d in scored[:n]]


def unused_opportunities(limit=40):
    """High-opportunity keywords that appear in NO listing's tags."""
    used = _tags_in_use()
    out = []
    for k in _all_keywords():
        if k["keyword"] in used:
            continue
        if any(k["keyword"] in u for u in used):
            continue
        out.append(k)
        if len(out) >= limit:
            break
    return out


def stats():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        n = conn.execute("SELECT COUNT(*) c FROM erank_keywords").fetchone()["c"]
        fits = conn.execute(
            "SELECT COUNT(*) c FROM erank_keywords WHERE LENGTH(keyword) <= ?",
            (ETSY_TAG_MAX_CHARS,)).fetchone()["c"]
        last = conn.execute(
            "SELECT MAX(imported_at) m FROM erank_keywords").fetchone()["m"] or ""
        conn.commit()
    finally:
        conn.close()
    return {"keywords": n, "tag_ready": fits, "last_import": last}


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
PAGE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eRank Keywords</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--sage:#77b28c;--teal:#00859b;--cyan:#00bfb8;--green:#38a66f;
        --lime:#bce194;--paleteal:#cce7eb;--offwhite:#ebf2e6;--cream:#f8ecdb;
        --lgray:#d9d9d6;--mgray:#b3b3b3;}
  *{box-sizing:border-box}
  body{font-family:'Outfit',system-ui,sans-serif;background:var(--offwhite);
       color:#15302b;margin:0;padding:2rem 1rem;}
  .wrap{max-width:960px;margin:0 auto;}
  h1{font-family:'Fraunces',serif;color:var(--teal);margin:0 0 .25rem;}
  h2{font-family:'Fraunces',serif;color:var(--green);font-size:1.1rem;
     margin:0 0 .7rem;}
  .sub{color:#4b665e;margin:0 0 1.25rem;font-size:.95rem;}
  .card{background:#fff;border:1px solid var(--lgray);border-radius:14px;
        padding:1.25rem;margin-bottom:1.25rem;box-shadow:0 1px 0 rgba(0,0,0,.03);}
  .bar{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;}
  .stat{background:var(--paleteal);border-radius:10px;padding:.6rem .9rem;
        min-width:118px;}
  .stat b{display:block;font-family:'Fraunces',serif;font-size:1.35rem;
          color:var(--teal);line-height:1.1;}
  .stat span{font-size:.78rem;color:#4b665e;}
  button,.btn{display:inline-block;border:none;border-radius:10px;
        padding:.6rem 1.1rem;font-family:inherit;font-weight:600;font-size:.92rem;
        cursor:pointer;text-decoration:none;}
  .primary{background:var(--teal);color:#fff;}
  .ghost{background:var(--cream);color:#7a5a23;}
  input[type=file]{font-family:inherit;font-size:.9rem;}
  table{width:100%;border-collapse:collapse;font-size:.88rem;}
  th{text-align:left;font-size:.74rem;text-transform:uppercase;letter-spacing:.05em;
     color:#4b665e;border-bottom:2px solid var(--lgray);padding:.5rem;}
  td{border-bottom:1px solid var(--offwhite);padding:.55rem .5rem;}
  tr:hover td{background:#fbfdf9;}
  .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}
  .opp{font-family:'Fraunces',serif;color:var(--teal);}
  .kw{font-weight:600;}
  .fits{background:var(--lime);color:#1d4d2b;border-radius:5px;
        padding:.1rem .35rem;font-size:.72rem;font-weight:600;}
  .long{background:var(--lgray);color:#5a6a65;border-radius:5px;
        padding:.1rem .35rem;font-size:.72rem;font-weight:600;}
  .hint{color:#6b7d77;font-size:.82rem;}
  ol{padding-left:1.2rem;color:#4b665e;font-size:.88rem;line-height:1.7;margin:0;}
  code{background:var(--cream);padding:.12rem .35rem;border-radius:5px;font-size:.84rem;}
  .flash{padding:.7rem .9rem;border-radius:9px;margin-bottom:.8rem;font-weight:500;}
  .flash.ok{background:var(--paleteal);color:#0a4a52;}
  .flash.error{background:#f7dcdc;color:#7a2222;}
  .links{margin-top:1rem;font-size:.9rem;} .links a{color:var(--teal);}
  .empty{padding:1.5rem;text-align:center;color:#4b665e;}
</style></head><body><div class="wrap">
  <h1>eRank Keywords</h1>
  <p class="sub">eRank has no API, so this reads their CSV exports. Once loaded,
     the Listing Health page suggests actual keywords for your empty tag slots
     instead of just counting them.</p>

  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat,msg in msgs %}<div class="flash {{cat}}">{{msg}}</div>{% endfor %}
  {% endwith %}

  <div class="card">
    <div class="bar">
      <div class="stat"><b>{{ st.keywords }}</b><span>keywords loaded</span></div>
      <div class="stat"><b>{{ st.tag_ready }}</b><span>fit 20-char limit</span></div>
      <div class="stat"><b>{{ opps|length }}</b><span>unused in your tags</span></div>
      <div style="flex:1"></div>
      <form method="post" action="{{ url_for('erank.import_page') }}"
            enctype="multipart/form-data" class="bar">
        <input type="file" name="file" accept=".csv,.tsv,.txt" required>
        <button class="primary" type="submit">Import CSV</button>
      </form>
    </div>
    {% if st.last_import %}
      <p class="hint" style="margin:.8rem 0 0">Last import {{ st.last_import }}
        &middot; <form method="post" action="{{ url_for('erank.clear_page') }}"
          style="display:inline" onsubmit="return confirm('Delete all imported eRank keywords?')">
          <button class="ghost" type="submit"
            style="padding:.25rem .6rem;font-size:.8rem">Clear all</button>
        </form></p>
    {% endif %}
  </div>

  {% if headers %}
  <div class="card">
    <h2>Headers found in that file</h2>
    <p class="hint">{{ headers|join(' · ') }}</p>
    <p class="hint">If the import found 0 keywords, paste this line into the chat
       and the alias list gets one more entry.</p>
  </div>
  {% endif %}

  {% if st.keywords == 0 %}
  <div class="card">
    <h2>Where to export from eRank</h2>
    <ol>
      <li><b>Keyword Tool</b> — search a seed term like <code>deadhead sticker</code>,
          then download the results. Best single source.</li>
      <li><b>Keyword Lists</b> — export any list you've saved.</li>
      <li><b>Listing Audit</b> — export; keyword columns are read, the rest ignored.</li>
      <li><b>Trend Buzz / Top Sellers</b> — export for seasonal terms.</li>
    </ol>
    <p class="hint" style="margin-top:.8rem">Import as many files as you like —
       they merge on the keyword, keeping the richest numbers. Relative values like
       "High"/"Low" are mapped to representative numbers so they still rank.</p>
  </div>
  {% endif %}

  <div class="card">
    <h2>Unused opportunities</h2>
    <p class="hint" style="margin:0 0 .8rem">High demand per unit of competition,
       and currently in <i>none</i> of your listings' tags. Green ones drop
       straight into a tag slot.</p>
    {% if not opps %}
      <div class="empty">
        {% if st.keywords == 0 %}Import an eRank export to see this.
        {% else %}Every imported keyword is already in use somewhere. Pull a
          broader export from the Keyword Tool.{% endif %}
      </div>
    {% else %}
      <table>
        <thead><tr>
          <th>Keyword</th><th class="num">Opportunity</th>
          <th class="num">Searches</th><th class="num">Competition</th>
          <th class="num">Chars</th>
        </tr></thead>
        <tbody>
        {% for k in opps %}
          <tr>
            <td class="kw">{{ k.display or k.keyword }}</td>
            <td class="num"><span class="opp">{{ '%.1f'|format(k.opportunity) }}</span></td>
            <td class="num">{{ '{:,.0f}'.format(k.avg_searches) }}</td>
            <td class="num">{{ '{:,.0f}'.format(k.competition) }}</td>
            <td class="num">
              {% if k.keyword|length <= 20 %}
                <span class="fits">{{ k.keyword|length }}</span>
              {% else %}
                <span class="long">{{ k.keyword|length }}</span>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    {% endif %}
  </div>

  <div class="links">
    <a href="/listings">← Listing Health</a> &nbsp;·&nbsp;
    <a href="/">Dashboard</a>
  </div>
</div></body></html>
"""


@erank_bp.route("/erank")
def erank_page():
    return render_template_string(PAGE_HTML, st=stats(),
                                  opps=unused_opportunities(40), headers=None)


@erank_bp.route("/erank/import", methods=["POST"])
def import_page():
    f = request.files.get("file")
    if not f or f.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("erank.erank_page"))
    try:
        res = import_erank_csv(f.read(), f.filename)
    except Exception as e:
        flash(f"Import failed: {e}", "error")
        return redirect(url_for("erank.erank_page"))

    if res["rows_new"] + res["rows_updated"] == 0:
        flash(f"Read {res['rows_seen']} rows but matched no keyword column — "
              f"headers are listed below.", "error")
    else:
        flash(f"Imported {res['rows_new']} new keywords, "
              f"{res['rows_updated']} updated ({res['rows_seen']} "
              f"{res['delim']}-separated rows read).", "ok")
    return render_template_string(PAGE_HTML, st=stats(),
                                  opps=unused_opportunities(40),
                                  headers=res["headers"])


@erank_bp.route("/erank/clear", methods=["POST"])
def clear_page():
    conn = get_db()
    try:
        conn.execute("DELETE FROM erank_keywords")
        conn.commit()
    finally:
        conn.close()
    flash("Cleared all imported eRank keywords.", "ok")
    return redirect(url_for("erank.erank_page"))
