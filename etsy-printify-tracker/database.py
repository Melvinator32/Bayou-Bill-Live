"""SQLite layer: connection (WAL + busy timeout), schema, settings helpers.

WAL mode + a busy timeout are what keep 'database is locked' from biting when a
connector sync and the web app touch the file close together. Keep the folder
OFF OneDrive — OneDrive fights with the WAL sidecar files.
"""
import sqlite3

from config import DB_PATH, DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS etsy_orders (
    order_id        TEXT PRIMARY KEY,
    channel         TEXT DEFAULT 'etsy',
    sale_date       TEXT,
    buyer           TEXT,
    order_shipping  REAL DEFAULT 0,
    order_sales_tax REAL DEFAULT 0,
    order_discount  REAL DEFAULT 0,
    order_total     REAL DEFAULT 0,
    currency        TEXT DEFAULT 'USD',
    date_paid       TEXT,
    date_shipped    TEXT
);

CREATE TABLE IF NOT EXISTS etsy_items (
    transaction_id  TEXT PRIMARY KEY,
    channel         TEXT DEFAULT 'etsy',
    order_id        TEXT,
    listing_id      TEXT,
    sku             TEXT DEFAULT '',
    item_name       TEXT,
    variations      TEXT,
    quantity        INTEGER DEFAULT 1,
    price           REAL DEFAULT 0,
    discount_amount REAL DEFAULT 0,
    item_total      REAL DEFAULT 0,
    sale_date       TEXT
);

CREATE TABLE IF NOT EXISTS sku_costs (
    sku          TEXT PRIMARY KEY,
    product_name TEXT,
    unit_cost    REAL DEFAULT 0,
    ship_cost    REAL DEFAULT 0,
    notes        TEXT,
    source       TEXT DEFAULT 'manual',
    updated_at   TEXT
);

-- Costs keyed by Etsy LISTING id (from Printify's product->listing link).
-- Used to match Printify costs to Etsy sales when SKUs are blank/mismatched.
CREATE TABLE IF NOT EXISTS listing_costs (
    listing_id   TEXT PRIMARY KEY,
    product_name TEXT,
    unit_cost    REAL DEFAULT 0,
    ship_cost    REAL DEFAULT 0,
    source       TEXT DEFAULT 'printify',
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    filename    TEXT,
    rows_seen   INTEGER,
    rows_new    INTEGER,
    rows_updated INTEGER,
    imported_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_sku ON etsy_items(sku);
CREATE INDEX IF NOT EXISTS idx_items_order ON etsy_items(order_id);
CREATE INDEX IF NOT EXISTS idx_items_date ON etsy_items(sale_date);
"""

# Columns we add defensively if an older db is opened (no-ops on a fresh db).
_MIGRATIONS = {
    "etsy_orders": {"channel": "TEXT DEFAULT 'etsy'",
                    "order_discount": "REAL DEFAULT 0"},
    "etsy_items": {"channel": "TEXT DEFAULT 'etsy'",
                   "discount_amount": "REAL DEFAULT 0"},
    "sku_costs": {"source": "TEXT DEFAULT 'manual'"},
}


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    for table, cols in _MIGRATIONS.items():
        try:
            have = _columns(conn, table)
        except sqlite3.OperationalError:
            continue
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    existing = {r["key"] for r in conn.execute("SELECT key FROM settings")}
    for k, v in DEFAULT_SETTINGS.items():
        if k not in existing:
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()


def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def save_settings(form):
    conn = get_db()
    for k, v in form.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    conn.commit()
    conn.close()
