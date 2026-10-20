"""
One-time helper: get an Etsy OAuth refresh token for the tracker.

Run it once, from the SAME folder as app.py:
    python get_etsy_token.py

It walks you through Etsy's sign-in, captures the result automatically, and
saves your keystring, shared secret, refresh token, and shop id into the app's
settings (tracker.db) so the Etsy connector is ready to sync.

PREREQUISITE — in Your Apps on Etsy (https://www.etsy.com/developers/your-apps),
open your app and add this EXACT Callback URL:
    http://localhost:3003/oauth/redirect
"""
import base64
import hashlib
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from database import init_db, save_settings

REDIRECT_URI = "http://localhost:3003/oauth/redirect"
REDIRECT_PORT = 3003
SCOPES = "transactions_r shops_r listings_r"          # read receipts (orders) + shop info
AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"


def make_pkce():
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)   # URL-safe, 43-128 chars
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/redirect":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = qs.get("code", [None])[0]
        self.server.auth_state = qs.get("state", [None])[0]
        self.server.auth_error = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = self.server.auth_code is not None
        msg = ("Etsy authorization received — you can close this tab and return "
               "to the terminal.") if ok else \
              ("Authorization failed: " + str(self.server.auth_error))
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;padding:2rem'>"
            f"<h3>{msg}</h3></body></html>".encode("utf-8"))

    def log_message(self, *args):  # keep the console quiet
        pass


def main():
    print("\n=== Etsy token setup ===\n")
    print("Find these on https://www.etsy.com/developers/your-apps "
          "(expand your app).\n")
    keystring = input("Paste your Etsy API Keystring: ").strip()
    secret = input("Paste your Etsy Shared Secret: ").strip()
    if not keystring or not secret:
        print("\nBoth the keystring and shared secret are required. Stopping.")
        sys.exit(1)

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": keystring,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    try:
        server = HTTPServer(("localhost", REDIRECT_PORT), _Handler)
    except OSError as e:
        print(f"\nCould not start the local listener on port {REDIRECT_PORT}: {e}")
        print("Close anything using that port and try again.")
        sys.exit(1)
    server.auth_code = server.auth_state = server.auth_error = None

    print("\nOpening your browser to authorize with Etsy.")
    print("If it doesn't open automatically, paste this URL into your browser:\n")
    print("  " + url + "\n")
    print(f"(Reminder: {REDIRECT_URI} must be a Callback URL in Your Apps.)\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("Waiting for you to approve access in the browser...")
    while server.auth_code is None and server.auth_error is None:
        server.handle_request()
    server.server_close()

    if server.auth_error:
        print("\nAuthorization failed:", server.auth_error)
        sys.exit(1)
    if server.auth_state != state:
        print("\nState mismatch — aborting for safety (possible CSRF).")
        sys.exit(1)

    print("Got the authorization code. Exchanging it for tokens...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": keystring,
        "redirect_uri": REDIRECT_URI,
        "code": server.auth_code,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        print("\nToken exchange failed:", resp.status_code, resp.text)
        sys.exit(1)
    tok = resp.json()
    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    if not refresh:
        print("\nNo refresh token returned by Etsy:", tok)
        sys.exit(1)

    # Best effort: detect the shop id automatically.
    shop_id = ""
    try:
        me = requests.get(f"{API_BASE}/users/me", headers={
            "x-api-key": f"{keystring}:{secret}",
            "Authorization": f"Bearer {access}",
        }, timeout=30)
        if me.status_code == 200:
            shop_id = str(me.json().get("shop_id") or "")
    except requests.exceptions.RequestException:
        pass
    if not shop_id:
        shop_id = input(
            "\nCouldn't auto-detect your shop id. Enter it now "
            "(or leave blank to add later on the Connectors page): ").strip()

    init_db()
    save_settings({
        "etsy_keystring": keystring,
        "etsy_shared_secret": secret,
        "etsy_refresh_token": refresh,
        "etsy_shop_id": shop_id,
    })

    print("\n  Success! Etsy credentials saved to the app.")
    print("  Shop ID:", shop_id or "(not set — add it on the Connectors page)")
    print("\nNext: start the app, go to Connectors -> Etsy (API) -> Sync now.\n")


if __name__ == "__main__":
    main()
