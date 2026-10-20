r"""Headless connector sync — for Windows Task Scheduler or manual runs.

    python sync.py                 # sync every configured connector
    python sync.py printify        # sync just Printify
    python sync.py etsy_api         # sync just Etsy

Schedule example (Task Scheduler action):
    Program:   C:\path\to\etsy-printify-tracker\.venv\Scripts\python.exe
    Arguments: sync.py
    Start in:  C:\path\to\etsy-printify-tracker
"""
import sys

from database import init_db, get_settings
from connectors import registry


def main():
    init_db()
    settings = get_settings()
    conns = registry.discover()
    targets = sys.argv[1:] or [
        k for k, c in conns.items()
        if c.implemented and c.is_configured(settings)]

    if not targets:
        print("Nothing to sync (no configured connectors).")
        return

    for key in targets:
        c = conns.get(key)
        if not c:
            print(f"[skip] unknown connector '{key}'")
            continue
        if not c.implemented:
            print(f"[skip] {c.label} not implemented")
            continue
        try:
            r = c.sync(settings)
            print(f"[ok]   {c.label}: {r}")
        except Exception as e:
            print(f"[fail] {c.label}: {e}")


if __name__ == "__main__":
    main()
