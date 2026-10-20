"""Static configuration: default fee model and database location."""

DB_PATH = "tracker.db"

# Etsy fee model used to ESTIMATE fees (Etsy's order data has no full fee
# breakdown). All editable on the Settings page.
DEFAULT_SETTINGS = {
    "transaction_fee_pct": "6.5",    # Etsy transaction fee, % of item revenue
    "processing_fee_pct": "3.0",     # payment processing, % of revenue
    "processing_fee_fixed": "0.25",  # payment processing, fixed per ORDER
    "listing_fee": "0.20",           # per listed item
    "offsite_ads_pct": "0.0",        # optional, % of revenue
    "currency": "USD",
}
