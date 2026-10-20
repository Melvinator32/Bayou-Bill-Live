r"""Cloud publish: sync Etsy + Printify and publish data.json, no local database.

Runs in the scheduled GitHub Actions job (.github/workflows/publish.yml). The
connectors and analytics need a SQLite file, so this builds a throwaway one in
a temp folder, fills its settings from environment variables (GitHub secrets),
runs every configured connector, and writes data.json at the repo root. The
temp database is never committed.

If any configured connector fails, nothing is written, so the live page keeps
its last good numbers instead of showing zeros.

Environment:
    ETSY_KEYSTRING, ETSY_SHARED_SECRET, ETSY_REFRESH_TOKEN, ETSY_SHOP_ID
    PRINTIFY_TOKEN, PRINTIFY_SHOP_ID (optional)
    TRANSACTION_FEE_PCT, PROCESSING_FEE_PCT, PROCESSING_FEE_FIXED,
    LISTING_FEE, OFFSITE_ADS_PCT, CURRENCY (optional fee-model overrides)
    NEW_REFRESH_TOKEN_FILE (optional): where to write a rotated Etsy refresh
        token so the workflow can update the ETSY_REFRESH_TOKEN secret
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import database
import publish_snapshot

ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

ENV_SETTINGS = {
    "ETSY_KEYSTRING": "etsy_keystring",
    "ETSY_SHARED_SECRET": "etsy_shared_secret",
    "ETSY_REFRESH_TOKEN": "etsy_refresh_token",
    "ETSY_SHOP_ID": "etsy_shop_id",
    "PRINTIFY_TOKEN": "printify_token",
    "PRINTIFY_SHOP_ID": "printify_shop_id",
    "TRANSACTION_FEE_PCT": "transaction_fee_pct",
    "PROCESSING_FEE_PCT": "processing_fee_pct",
    "PROCESSING_FEE_FIXED": "processing_fee_fixed",
    "LISTING_FEE": "listing_fee",
    "OFFSITE_ADS_PCT": "offsite_ads_pct",
    "CURRENCY": "currency",
}


class PublishError(RuntimeError):
    pass


def settings_from_env(environ):
    return {key: environ[var].strip() for var, key in ENV_SETTINGS.items()
            if (environ.get(var) or "").strip()}


@contextmanager
def capture_refresh_tokens(target, sink):
    """Record refresh tokens Etsy hands back while target.post is in use.

    Etsy may issue a new refresh token on each refresh; keeping the newest one
    stops the stored secret from expiring 90 days after it was first issued.
    """
    original = target.post

    def post(url, *args, **kwargs):
        resp = original(url, *args, **kwargs)
        if url == ETSY_TOKEN_URL:
            try:
                token = (resp.json() or {}).get("refresh_token")
            except ValueError:
                token = None
            if token:
                sink.append(token)
        return resp

    target.post = post
    try:
        yield sink
    finally:
        target.post = original


def run(environ, out_dir, db_path, connectors=None, today=None):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database.DB_PATH = str(db_path)
    database.init_db()
    database.save_settings(settings_from_env(environ))
    settings = database.get_settings()

    if connectors is None:
        from connectors import registry
        connectors = registry.discover()

    # registry.discover() orders sales before cost; Printify reads Etsy's rows.
    ready = [c for c in connectors.values()
             if c.implemented and c.is_configured(settings)]
    if not any(c.kind == "sales" for c in ready):
        raise PublishError("No sales connector is configured; check the ETSY_* secrets.")

    for c in sorted(ready, key=lambda c: c.kind != "sales"):
        try:
            result = c.sync(settings)
        except Exception as e:
            # Etsy and Printify explain 4xx errors in the body (e.g. invalid_grant).
            body = getattr(getattr(e, "response", None), "text", "") or ""
            detail = f" | {body.strip()[:500]}" if body.strip() else ""
            raise PublishError(f"{c.label} sync failed: {e}{detail}") from e
        print(f"[ok] {c.label}: {result}")

    snapshot = publish_snapshot.build_snapshot(today=today)
    return publish_snapshot.write_snapshot(snapshot, out_dir)


def main():
    import requests

    repo_root = Path(__file__).resolve().parent.parent
    rotated = []
    try:
        with tempfile.TemporaryDirectory() as tmp, capture_refresh_tokens(requests, rotated):
            out = run(os.environ, out_dir=repo_root, db_path=Path(tmp) / "tracker.db")
    except PublishError as e:
        print(f"[fail] {e}\nNothing published; the live page keeps its last numbers.")
        return 1
    print(f"Wrote {out}")

    token_file = os.environ.get("NEW_REFRESH_TOKEN_FILE")
    if token_file and rotated and rotated[-1] != os.environ.get("ETSY_REFRESH_TOKEN"):
        Path(token_file).write_text(rotated[-1])
        print("Etsy issued a new refresh token; saved for the secret update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
