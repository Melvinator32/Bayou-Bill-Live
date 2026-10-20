"""One-time helper: print the values to paste into GitHub secrets.

Run on your PC from this folder, where your working tracker.db is:
    python show_github_secrets.py

Then add each NAME / value at GitHub: repo Settings -> Secrets and variables
-> Actions -> New repository secret. Don't paste the output anywhere public.
"""
from database import get_settings

SECRETS = [
    ("ETSY_KEYSTRING", "etsy_keystring"),
    ("ETSY_SHARED_SECRET", "etsy_shared_secret"),
    ("ETSY_REFRESH_TOKEN", "etsy_refresh_token"),
    ("ETSY_SHOP_ID", "etsy_shop_id"),
    ("PRINTIFY_TOKEN", "printify_token"),
    ("PRINTIFY_SHOP_ID", "printify_shop_id"),
]

if __name__ == "__main__":
    s = get_settings()
    for name, key in SECRETS:
        value = s.get(key) or ""
        print(f"{name}\n    {value or '(not set; connect it in the app first, or skip if optional)'}\n")
