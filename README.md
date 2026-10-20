# Bayou-Bill-Live

The Bayou Bill shop tracker: a local Etsy + Printify profit app, plus a public
page showing the shop's real numbers. The portfolio demo at
jamesmcknight.me/demos/bayou-bill-shop-tracker/ stays on made-up data.

- `etsy-printify-tracker/`: the Flask app. It runs on your PC and syncs Etsy and Printify into `tracker.db`.
- `index.html`: the public dashboard, served by GitHub Pages. It reads `data.json`.
- `data.json`: written by `publish_snapshot.py`. Never edit it by hand.

## Never commit tracker.db

`tracker.db` holds the Etsy keystring, shared secret and refresh token, the
Printify token, and buyer names. `.gitignore` excludes it. Don't upload the
app folder through the GitHub website, because web uploads ignore `.gitignore`.
Commit from a clone instead.

## What the public page shows

Only totals and product titles: revenue, orders, average order value,
Printify product cost, estimated Etsy fees, profit, daily and monthly revenue,
top products, order status counts, and the 10 most recent orders (date,
products, units, status, total).

Never published: buyer names or addresses, order / transaction / listing ids,
SKUs, variations (they can contain personalization text), or any API key.

## Publishing new numbers

Run the app from your clone of this repo (`etsy-printify-tracker/`), then
double-click `publish.bat` there, or:

    cd etsy-printify-tracker
    python publish_snapshot.py --sync --repo ..

It syncs Etsy and Printify, writes `data.json` at the repo root, commits only
that file and pushes. The page updates within a minute or two. Git must be on
your PATH (Git for Windows). To run it on a schedule, point Windows Task
Scheduler at `publish.bat`.

## Tests

    cd etsy-printify-tracker
    pip install pytest
    python -m pytest test_publish_snapshot.py
