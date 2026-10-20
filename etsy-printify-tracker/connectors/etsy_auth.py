"""
In-app Etsy connect flow (OAuth 2.0 + PKCE), served by the tracker itself.
No separate script or terminal needed — everything happens in the browser.

Adds three pages to the running app:
  /etsy/setup     paste your keystring + shared secret, then click Connect
  /etsy/connect   starts the Etsy sign-in (redirects you to Etsy)
  /etsy/callback  Etsy returns here; we store the refresh token + shop id

Wire it up in app.py with two lines:
    from etsy_auth import etsy_bp
    app.register_blueprint(etsy_bp)

Prerequisite — in Your Apps on Etsy (https://www.etsy.com/developers/your-apps),
open your app and add this EXACT Callback URL:
    http://localhost:2298/etsy/callback
"""
import base64
import hashlib
import secrets
import urllib.parse

import requests
from flask import (Blueprint, request, redirect, url_for, flash,
                   render_template_string)

from database import get_settings, save_settings

etsy_bp = Blueprint("etsy_auth", __name__)

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API_BASE = "https://openapi.etsy.com/v3/application"
REDIRECT_URI = "http://localhost:2298/etsy/callback"
SCOPES = "transactions_r shops_r"


def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


SETUP_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Etsy</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--sage:#77b28c;--teal:#00859b;--cyan:#00bfb8;--green:#38a66f;
        --lime:#bce194;--paleteal:#cce7eb;--offwhite:#ebf2e6;--cream:#f8ecdb;
        --lgray:#d9d9d6;--mgray:#b3b3b3;}
  *{box-sizing:border-box}
  body{font-family:'Outfit',system-ui,sans-serif;background:var(--offwhite);
       color:#15302b;margin:0;padding:2rem 1rem;}
  .wrap{max-width:640px;margin:0 auto;}
  h1{font-family:'Fraunces',serif;color:var(--teal);margin:0 0 .25rem;}
  .sub{color:#4b665e;margin:0 0 1.5rem;}
  .card{background:#fff;border:1px solid var(--lgray);border-radius:14px;
        padding:1.5rem;margin-bottom:1.25rem;box-shadow:0 1px 0 rgba(0,0,0,.03);}
  .badge{display:inline-block;padding:.3rem .7rem;border-radius:999px;
         font-weight:600;font-size:.85rem;}
  .ok{background:var(--lime);color:#1d4d2b;}
  .no{background:var(--cream);color:#7a5a23;}
  label{display:block;font-weight:600;margin:.9rem 0 .3rem;font-size:.92rem;}
  input[type=text],input[type=password]{width:100%;padding:.6rem .7rem;
        border:1px solid var(--mgray);border-radius:9px;font-size:.95rem;
        font-family:inherit;}
  .hint{color:#6b7d77;font-size:.82rem;margin-top:.3rem;}
  .step{font-family:'Fraunces',serif;font-size:1.1rem;color:var(--green);
        margin:.2rem 0 .6rem;}
  button,.btn{display:inline-block;border:none;border-radius:10px;
        padding:.7rem 1.2rem;font-family:inherit;font-weight:600;font-size:.95rem;
        cursor:pointer;text-decoration:none;margin-top:1rem;}
  .primary{background:var(--teal);color:#fff;}
  .connect{background:var(--cyan);color:#063;}
  .connect.disabled{background:var(--lgray);color:#888;pointer-events:none;}
  .links{margin-top:1rem;font-size:.9rem;}
  .links a{color:var(--teal);}
  .flash{padding:.7rem .9rem;border-radius:9px;margin-bottom:.8rem;font-weight:500;}
  .flash.ok{background:var(--paleteal);color:#0a4a52;}
  .flash.error{background:#f7dcdc;color:#7a2222;}
  code{background:var(--cream);padding:.15rem .4rem;border-radius:5px;
       font-size:.85rem;}
</style></head><body><div class="wrap">
  <h1>Connect Etsy</h1>
  <p class="sub">Paste your two Etsy keys, then click Connect. The app handles
     the sign-in and stores everything — no terminal needed.</p>

  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat,msg in msgs %}<div class="flash {{cat}}">{{msg}}</div>{% endfor %}
  {% endwith %}

  <div class="card">
    Status:
    {% if connected %}<span class="badge ok">Connected ✓</span>
    {% else %}<span class="badge no">Not connected yet</span>{% endif %}
    {% if s.etsy_shop_id %}<span class="hint">Shop ID: {{s.etsy_shop_id}}</span>{% endif %}
  </div>

  <div class="card">
    <div class="step">Step 1 — Paste your keys</div>
    <p class="hint">Find both on
       <a href="https://www.etsy.com/developers/your-apps" target="_blank">Your Apps</a>
       (expand your app).</p>
    <form method="post">
      <label>API Keystring</label>
      <input type="text" name="etsy_keystring" value="{{s.etsy_keystring or ''}}"
             placeholder="e.g. 1aa2bb33c44d55eeeeee6fff" autocomplete="off">
      <label>Shared Secret</label>
      <input type="password" name="etsy_shared_secret"
             placeholder="{{'•••••• (saved)' if s.etsy_shared_secret else 'paste secret'}}"
             autocomplete="off">
      <label>Shop ID <span class="hint">(optional — auto-detected on connect)</span></label>
      <input type="text" name="etsy_shop_id" value="{{s.etsy_shop_id or ''}}"
             placeholder="leave blank to auto-detect" autocomplete="off">
      <button class="primary" type="submit">Save keys</button>
    </form>
  </div>

  <div class="card">
    <div class="step">Step 2 — Connect</div>
    <p class="hint">Make sure <code>{{redirect_uri}}</code> is listed as a
       Callback URL in Your Apps first.</p>
    {% if have_keys %}
      <a class="btn connect" href="{{ url_for('etsy_auth.etsy_connect') }}">
        {{ 'Reconnect with Etsy' if connected else 'Connect with Etsy' }}</a>
    {% else %}
      <span class="btn connect disabled">Connect with Etsy</span>
      <p class="hint">Save your keystring and shared secret above first.</p>
    {% endif %}
  </div>

  <div class="links">
    <a href="/connectors">← Back to Connectors</a> &nbsp;·&nbsp;
    <a href="/">Dashboard</a>
  </div>
</div></body></html>
"""


@etsy_bp.route("/etsy/setup", methods=["GET", "POST"])
def etsy_setup():
    if request.method == "POST":
        updates = {}
        for k in ("etsy_keystring", "etsy_shared_secret", "etsy_shop_id"):
            v = request.form.get(k, "").strip()
            if v:
                updates[k] = v
        if updates:
            save_settings(updates)
            flash("Saved your Etsy keys.", "ok")
        return redirect(url_for("etsy_auth.etsy_setup"))

    s = get_settings()
    return render_template_string(
        SETUP_HTML, s=s,
        connected=bool(s.get("etsy_refresh_token")),
        have_keys=bool(s.get("etsy_keystring") and s.get("etsy_shared_secret")),
        redirect_uri=REDIRECT_URI)


@etsy_bp.route("/etsy/connect")
def etsy_connect():
    s = get_settings()
    if not (s.get("etsy_keystring") and s.get("etsy_shared_secret")):
        flash("Enter your keystring and shared secret first.", "error")
        return redirect(url_for("etsy_auth.etsy_setup"))

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    # Store the one-time PKCE values server-side (not in the cookie) so the
    # localhost/127.0.0.1 host difference on the callback can't lose them.
    save_settings({"etsy_oauth_state": state, "etsy_pkce_verifier": verifier})

    params = {
        "response_type": "code",
        "client_id": s["etsy_keystring"],
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(AUTH_URL + "?" + urllib.parse.urlencode(params))


@etsy_bp.route("/etsy/callback")
def etsy_callback():
    s = get_settings()

    if request.args.get("error"):
        flash(f"Etsy authorization failed: {request.args.get('error')}", "error")
        return redirect(url_for("etsy_auth.etsy_setup"))

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != s.get("etsy_oauth_state"):
        flash("Authorization didn't match — please click Connect again.", "error")
        return redirect(url_for("etsy_auth.etsy_setup"))

    verifier = s.get("etsy_pkce_verifier")
    try:
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": s["etsy_keystring"],
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        }, timeout=30)
        resp.raise_for_status()
        tok = resp.json()
    except Exception as e:
        flash(f"Token exchange failed: {e}", "error")
        return redirect(url_for("etsy_auth.etsy_setup"))

    refresh = tok.get("refresh_token")
    access = tok.get("access_token")
    if not refresh:
        flash("Etsy didn't return a refresh token — please try again.", "error")
        return redirect(url_for("etsy_auth.etsy_setup"))

    updates = {"etsy_refresh_token": refresh,
               # clear the one-time values
               "etsy_oauth_state": "", "etsy_pkce_verifier": ""}

    # Best effort: auto-detect the shop id if we don't have it yet.
    if not s.get("etsy_shop_id"):
        try:
            me = requests.get(f"{API_BASE}/users/me", headers={
                "x-api-key": f"{s['etsy_keystring']}:{s['etsy_shared_secret']}",
                "Authorization": f"Bearer {access}",
            }, timeout=30)
            if me.status_code == 200:
                sid = str(me.json().get("shop_id") or "")
                if sid:
                    updates["etsy_shop_id"] = sid
        except requests.exceptions.RequestException:
            pass

    save_settings(updates)
    flash("Etsy connected! Head to Connectors and hit Sync.", "ok")
    return redirect(url_for("etsy_auth.etsy_setup"))
