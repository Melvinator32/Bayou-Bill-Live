# Bayou-Bill-Live

The Bayou Bill shop tracker: a local Etsy + Printify profit app, plus a public
page showing the shop's real numbers. The portfolio demo at
jamesmcknight.me/demos/bayou-bill-shop-tracker/ stays on made-up data.

- `etsy-printify-tracker/`: the Flask app. It runs on your PC and syncs Etsy and Printify into `tracker.db`.
- `index.html`: the public dashboard, deployed as a Cloudflare Worker (`wrangler.jsonc`). It reads `data.json`.
- `.assetsignore`: keeps the app source and repo files out of the deployment; only the page and `data.json` are served.
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

## How the numbers update

A scheduled GitHub Action (`.github/workflows/publish.yml`) runs every 6 hours.
It syncs Etsy and Printify through the app's own connectors into a throwaway
database, builds `data.json` with `analytics.py`, and commits it. Cloudflare
redeploys on that push. Nothing runs on your PC. To update right away, go to
Actions -> Publish shop numbers -> Run workflow.

If a sync fails, nothing is published and the page keeps its last numbers.
The run shows up red in the Actions tab, and GitHub emails you.

### One-time setup: GitHub secrets

Repo Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret | Value |
|---|---|
| `ETSY_KEYSTRING` | Etsy app keystring |
| `ETSY_SHARED_SECRET` | Etsy app shared secret |
| `ETSY_REFRESH_TOKEN` | From the app's Etsy connect flow |
| `ETSY_SHOP_ID` | Your Etsy shop id |
| `PRINTIFY_TOKEN` | Printify personal access token |
| `PRINTIFY_SHOP_ID` | Optional; auto-detected |
| `SECRETS_PAT` | Optional; see below |

`python show_github_secrets.py` (run once in the app folder on your PC) prints
every value the app already has, ready to paste.

Etsy refresh tokens expire after 90 days. When Etsy issues a new one, the job
saves it to `ETSY_REFRESH_TOKEN`, but only if `SECRETS_PAT` is set: a
fine-grained GitHub token for this repo with **Secrets: Read and write**.
Without it, reconnect Etsy in the app every 90 days and update the secret.

Product costs come from Printify. Costs typed by hand on the app's Costs page
live only in your local database and aren't used by the scheduled job.

### Publishing from your PC instead

`publish.bat` (or `python publish_snapshot.py --sync --repo ..`) does the
same thing from a local clone, using the app's own `tracker.db`.

## Tests

    cd etsy-printify-tracker
    pip install pytest
    python -m pytest
