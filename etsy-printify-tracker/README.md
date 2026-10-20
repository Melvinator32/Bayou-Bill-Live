# Bayou Bill — Etsy + Printify Profit Tracker

Local Flask app. Pulls **orders from Etsy** and **costs from Printify** via their
APIs, matches them by SKU, and shows profit. Runs only on your machine at
http://localhost:2298 — data lives in `tracker.db`.

## First run
1. Double-click `run.bat` (creates a venv and installs deps the first time).
2. Open http://localhost:2298

## Connect your stores
**Etsy** (orders): Connectors page → *Open Etsy setup* → paste your keystring +
shared secret → *Connect with Etsy*. First add this callback in Etsy "Your Apps":
`http://localhost:2298/etsy/callback`

**Printify** (costs): Connectors page → Printify card → paste your Personal Access
Token (Printify → My Profile → Connections → Generate) → Save → Sync now.

Then sync both and check the Dashboard.

## Notes
- Etsy profit uses the **fee estimate** on the Settings page (Etsy's data lacks a
  full fee breakdown). Printify costs are real.
- SKUs must match between Etsy and Printify for costs to attach.
- Keep this folder **off OneDrive** (WAL file conflicts).
- `python sync.py` syncs all configured connectors headlessly (Task Scheduler).
