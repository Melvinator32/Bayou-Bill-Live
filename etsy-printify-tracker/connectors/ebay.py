"""eBay connector — TEMPLATE / not implemented yet.

When you get eBay API access (developer.ebay.com -> Application Keys), fill in
_collect() using the Sell Fulfillment API getOrders endpoint. eBay returns REAL
per-order fees, so unlike Etsy this channel won't need the fee-model estimate.

Endpoints to use later:
  token:  https://api.ebay.com/identity/v1/oauth2/token  (refresh_token grant)
  orders: https://api.ebay.com/sell/fulfillment/v1/order?filter=...
  scope:  https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly
"""
from connectors import Connector, SettingField


class EbayConnector(Connector):
    key = "ebay_api"
    label = "eBay (API)"
    kind = "sales"
    docs_url = "https://developer.ebay.com/api-docs/sell/fulfillment/overview.html"
    implemented = False
    settings_fields = [
        SettingField("ebay_app_id", "App ID (Client ID)", "text"),
        SettingField("ebay_cert_id", "Cert ID (Client Secret)", "password"),
        SettingField("ebay_refresh_token", "OAuth Refresh Token", "password"),
        SettingField("ebay_marketplace", "Marketplace", "text",
                     "e.g. EBAY_US"),
    ]

    def test(self, settings):
        return {"ok": False,
                "message": "eBay connector is a template — not implemented yet."}

    def sync(self, settings):
        raise RuntimeError("eBay connector not implemented yet.")
