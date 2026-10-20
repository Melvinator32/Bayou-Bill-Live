"""Optional CSV import — a fallback for when you'd rather load a file than sync
the API. Not required if you use the Etsy/Printify connectors.

import_etsy_csv:  Etsy "Sold Order Items" CSV -> etsy_orders / etsy_items
import_costs_csv: simple SKU cost CSV -> sku_costs (source='manual')
"""
import csv
import io
from datetime import datetime

from dateutil import parser as dateparser

from database import get_db


def _norm(h):
    return (h or "").strip().lower().replace(" ", "_").replace("-", "_")


def _money(v):
    if v is None:
        return 0.0
    s = str(v).strip().replace("$", "").replace(",", "")
    if s in ("", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _date(v):
    if not v:
        return ""
    try:
        return dateparser.parse(str(v)).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return str(v)[:10]


def _pick(row, *aliases, default=""):
    for a in aliases:
        if a in row and str(row[a]).strip() != "":
            return row[a]
    return default


def _read_rows(data, filename):
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        yield {_norm(k): v for k, v in raw.items()}


def import_etsy_csv(data, filename="upload.csv"):
    rows_seen = rows_new = rows_updated = 0
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        for row in _read_rows(data, filename):
            rows_seen += 1
            order_id = str(_pick(row, "order_id", "order")).strip()
            txn_id = str(_pick(row, "transaction_id", "transaction")).strip()
            if not txn_id:
                continue
            sale_date = _date(_pick(row, "sale_date", "date_paid", "paid_on"))
            qty = int(_money(_pick(row, "quantity", "qty", default="1")) or 1)
            price = _money(_pick(row, "price"))
            item_total = _money(_pick(row, "item_total", default="")) or price * qty
            sku = str(_pick(row, "sku")).strip()

            # upsert order
            conn.execute("""
                INSERT INTO etsy_orders
                    (order_id, channel, sale_date, buyer, order_shipping,
                     order_sales_tax, order_discount, order_total, currency,
                     date_paid, date_shipped)
                VALUES (?,'etsy',?,?,?,?,?,?,?,?,?)
                ON CONFLICT(order_id) DO UPDATE SET
                    sale_date=COALESCE(NULLIF(excluded.sale_date,''), sale_date),
                    buyer=COALESCE(NULLIF(excluded.buyer,''), buyer),
                    order_shipping=excluded.order_shipping,
                    order_sales_tax=excluded.order_sales_tax,
                    order_total=excluded.order_total""",
                (order_id or txn_id, sale_date,
                 _pick(row, "full_name", "buyer", "ship_name"),
                 _money(_pick(row, "order_shipping", "shipping")),
                 _money(_pick(row, "order_sales_tax", "sales_tax", "tax")),
                 _money(_pick(row, "discount_amount", "discount")),
                 _money(_pick(row, "order_total", "order_value")),
                 _pick(row, "currency", default="USD"),
                 _date(_pick(row, "date_paid")), _date(_pick(row, "date_shipped"))))

            exists = conn.execute(
                "SELECT 1 FROM etsy_items WHERE transaction_id=?", (txn_id,)).fetchone()
            conn.execute("""
                INSERT INTO etsy_items
                    (transaction_id, channel, order_id, listing_id, sku, item_name,
                     variations, quantity, price, discount_amount, item_total, sale_date)
                VALUES (?,'etsy',?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    order_id=excluded.order_id, listing_id=excluded.listing_id,
                    sku=excluded.sku, item_name=excluded.item_name,
                    variations=excluded.variations, quantity=excluded.quantity,
                    price=excluded.price, item_total=excluded.item_total,
                    sale_date=excluded.sale_date""",
                (txn_id, order_id or txn_id, str(_pick(row, "listing_id")).strip(),
                 sku, _pick(row, "item_name", "title"),
                 _pick(row, "variations", "variation"), qty, price,
                 _money(_pick(row, "discount_amount", "discount")),
                 item_total, sale_date))
            if exists:
                rows_updated += 1
            else:
                rows_new += 1
        conn.execute("""INSERT INTO import_log
            (source, filename, rows_seen, rows_new, rows_updated, imported_at)
            VALUES ('etsy-csv',?,?,?,?,?)""",
            (filename, rows_seen, rows_new, rows_updated, now))
        conn.commit()
    finally:
        conn.close()
    return {"rows_seen": rows_seen, "rows_new": rows_new, "rows_updated": rows_updated}


def import_costs_csv(data, filename="costs.csv"):
    rows_new = rows_updated = 0
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        for row in _read_rows(data, filename):
            sku = str(_pick(row, "sku")).strip()
            if not sku:
                continue
            exists = conn.execute(
                "SELECT 1 FROM sku_costs WHERE sku=?", (sku,)).fetchone()
            conn.execute("""
                INSERT INTO sku_costs
                    (sku, product_name, unit_cost, ship_cost, notes, source, updated_at)
                VALUES (?,?,?,?,?,'manual',?)
                ON CONFLICT(sku) DO UPDATE SET
                    product_name=excluded.product_name, unit_cost=excluded.unit_cost,
                    ship_cost=excluded.ship_cost, notes=excluded.notes,
                    source='manual', updated_at=excluded.updated_at""",
                (sku, _pick(row, "product_name", "name", "title"),
                 _money(_pick(row, "unit_cost", "cost")),
                 _money(_pick(row, "ship_cost", "shipping")),
                 _pick(row, "notes"), now))
            if exists:
                rows_updated += 1
            else:
                rows_new += 1
        conn.commit()
    finally:
        conn.close()
    return {"rows_new": rows_new, "rows_updated": rows_updated}
