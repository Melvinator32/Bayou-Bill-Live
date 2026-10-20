"""Read-only diagnostic — inspects tracker.db and reports why costs aren't
matching. Changes NOTHING. Run from the app folder:

    python diagnose.py
"""
import sqlite3

DB = "tracker.db"


def tables(c):
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def count(c, t):
    try:
        return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.OperationalError:
        return "(no such table)"


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    tabs = tables(c)

    print("\n================ TABLES PRESENT ================")
    for t in ("etsy_orders", "etsy_items", "sku_costs", "listing_costs",
              "import_log", "settings"):
        flag = "OK " if t in tabs else "MISSING"
        print(f"  [{flag}] {t}: {count(c, t)} rows")
    if "listing_costs" not in tabs:
        print("\n>>> listing_costs table is MISSING — the database.py update did")
        print(">>> NOT get applied. Re-copy the 5 update files into the app folder.")
        return

    print("\n================ RECENT SYNCS / IMPORTS ================")
    try:
        for r in c.execute("SELECT source, filename, rows_seen, rows_new, "
                           "rows_updated, imported_at FROM import_log "
                           "ORDER BY id DESC LIMIT 6"):
            print(f"  {r['imported_at']}  {r['source']:<12} seen={r['rows_seen']} "
                  f"new={r['rows_new']} upd={r['rows_updated']}  {r['filename']}")
    except sqlite3.OperationalError as e:
        print("  (could not read import_log:", e, ")")

    print("\n================ SAMPLE ETSY ITEMS (what sold) ================")
    print("  listing_id              | sku                     | item_name")
    for r in c.execute("SELECT listing_id, sku, item_name FROM etsy_items LIMIT 6"):
        print(f"  {str(r['listing_id'] or ''):<23} | {str(r['sku'] or ''):<23} | "
              f"{(r['item_name'] or '')[:32]}")

    print("\n================ SAMPLE LISTING COSTS (from Printify) ================")
    rows = list(c.execute("SELECT listing_id, product_name, unit_cost, ship_cost "
                          "FROM listing_costs LIMIT 6"))
    if not rows:
        print("  (empty - Printify sync wrote NO listing costs)")
    for r in rows:
        print(f"  {str(r['listing_id'] or ''):<23} | u={r['unit_cost']} s={r['ship_cost']} | "
              f"{(r['product_name'] or '')[:32]}")

    print("\n================ SAMPLE SKU COSTS ================")
    rows = list(c.execute("SELECT sku, unit_cost, source FROM sku_costs LIMIT 6"))
    if not rows:
        print("  (empty)")
    for r in rows:
        print(f"  {str(r['sku'] or ''):<23} | u={r['unit_cost']} | {r['source']}")

    print("\n================ MATCH CHECK ================")
    total_items = c.execute("SELECT COUNT(*) FROM etsy_items").fetchone()[0]
    matched = c.execute("""
        SELECT COUNT(*) FROM etsy_items i
        WHERE EXISTS (SELECT 1 FROM sku_costs sc WHERE sc.sku=i.sku AND i.sku!='')
           OR EXISTS (SELECT 1 FROM listing_costs lc WHERE lc.listing_id!=''
                      AND (lc.listing_id=i.listing_id OR lc.listing_id=i.sku))
    """).fetchone()[0]
    print(f"  etsy_items total: {total_items}")
    print(f"  matched to a cost: {matched}")
    print(f"  UNMATCHED: {total_items - matched}")

    print("\n  --- a few UNMATCHED items (compare these ids to listing_costs above) ---")
    for r in c.execute("""
        SELECT i.listing_id, i.sku, i.item_name FROM etsy_items i
        WHERE NOT (
            EXISTS (SELECT 1 FROM sku_costs sc WHERE sc.sku=i.sku AND i.sku!='')
            OR EXISTS (SELECT 1 FROM listing_costs lc WHERE lc.listing_id!=''
                       AND (lc.listing_id=i.listing_id OR lc.listing_id=i.sku)))
        LIMIT 5"""):
        print(f"    listing_id={r['listing_id']!r}  sku={r['sku']!r}  "
              f"name={(r['item_name'] or '')[:28]!r}")

    c.close()
    print("\nDone. Copy this whole output back to the chat.\n")


if __name__ == "__main__":
    main()
