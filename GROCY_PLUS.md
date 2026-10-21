# Grocy+

A self-hosted household grocery system for a two-person home in Kaunas: upstream
[Grocy](https://github.com/grocy/grocy) for stock, shopping list, recipes and meal plan, plus a
**companion** service that uses the Claude API for the parts Grocy lacks: nutrition capture from
barcodes, label photos and market produce, waste-aware cooking (partly used packs get opened and
their due dates recalculated), a weekly meal plan adapted to what is at home, cost/nutrition
reports and a daily "use these first" alert.

This branch (`plus`) is upstream Grocy **unchanged** plus new files only:

| Path | What |
|---|---|
| `deploy/grocy/` | Dockerfile, entrypoint, backup script, Apache/PHP config for the Grocy service |
| `railway.json` | Railway build settings for the Grocy service (read automatically at the repo root) |
| `companion/` | The companion service (FastAPI). API reference: [`companion/README.md`](companion/README.md) |
| `.github/workflows/` | Weekly upstream sync, companion CI |
| `.dockerignore`, `GROCY_PLUS.md` | Build context rules, this document |

No upstream file is modified, so upstream merges never conflict.

## Architecture

```
 iPhone: Grocy Mobile ─┐                              ┌─ ntfy app (both phones)
 Android: Grocy Android ┼─► grocy  (PHP 8.5, Apache)  │
                        │     │  volume /var/www/html/data (grocy.db, photos, config.php)
 Phone browser (PWA) ───┼─► companion (FastAPI) ──────┤──► ntfy topic: daily 08:00 alert
                              │  volume /data (companion.db: events, Claude cost log, plans)
                              ├──► Grocy REST API (GROCY-API-KEY, private network)
                              ├──► Claude API (Sonnet 5.5 for vision/planning, Haiku 5.5 for small tasks)
                              ├──► Open Food Facts (barcodes)
                              ├──► Fineli, Finnish food composition database (produce nutrition)
                              └──► USDA FoodData Central (produce nutrition)
```

Both phones talk to the same Grocy server, which keeps them in sync. The companion's own page is
for the things the Grocy apps can't do; browsing stock and the shopping list stays in the Grocy apps.

Data model in Grocy:
* Food products use **g, ml or piece** as stock unit, so "500 g of a 1 kg pack" is representable.
  When the pack size is known, the purchase unit is *Pack* with a product-specific Pack → g/ml
  conversion, so scanning "1 pack" in the Grocy apps adds e.g. 1000 g.
* Nutrition per 100 g/ml lives in product userfields `kcal_100`, `protein_100`, `fat_100`,
  `satfat_100`, `carbs_100`, `sugar_100`, `fiber_100`, `salt_100`, with `nutrition_source`
  (`open_food_facts | label_photo | usda | manual`), `net_amount_per_pack`, `net_unit` and the
  `label_photo` image. Grocy's native `calories` (kcal per stock unit) is filled too, so Grocy's own
  recipe views show calories.
* `treat_opened_as_out_of_stock = 0` on food (Grocy's default is 1, which would make an opened pasta
  pack count as "out of stock"). New products created in the Grocy apps also default to 0 because the
  entrypoint sets that preset in `config.php`; `/setup` fixes older products (products in a group
  named like "household"/"cleaning"/"non-food" are left alone).
* Prices are stored per stock unit with the shopping location, so Grocy's price history per store works.

## Deploying to Railway (complete walkthrough)

Allow about 45 minutes. Replace `<grocy-domain>` / `<companion-domain>` with the domains Railway gives
you in steps 2.6 and 5.6. Railway renames menus from time to time; if a label differs, look for the
same setting nearby.

**Railway basics used below:** a *project* is a canvas with boxes (*services*). Click a service box to
open its panel with the tabs **Deployments · Variables · Metrics · Settings** (plus **Backups** once it
has a volume). Railway collects your edits as *staged changes*; nothing happens until you press
**Deploy** (purple button at the top of the canvas).

### Step 0 — before you start (done ✅ / to do)
- ✅ GitHub default branch is `plus`; ✅ Actions may create pull requests.
- Get the keys from [Getting the keys and secrets](#getting-the-keys-and-secrets): Anthropic API key,
  USDA key, an ntfy topic name and a `COMPANION_TOKEN`. (The Grocy API key comes in step 3.)

### Step 1 — create the project
1. Open <https://railway.com/dashboard> and log in (same account as the bank app).
2. **+ New** (top right) → **Deploy from GitHub repo**.
3. If `brunopratus/grocy` isn't listed: **Configure GitHub App** → give Railway access to the
   `brunopratus/grocy` repository → back in Railway pick it.
4. Railway creates a project with one service and immediately tries to build it. That first build can
   fail or build the wrong thing — fine, we configure it now. Rename the project (top left, project
   name → **Settings → Project name**) to `grocy-plus`.

### Step 2 — the `grocy` service
Click the service box → **Settings** tab:
1. **Service name** (top of Settings): `grocy`. The name matters: it becomes the private address
   `grocy.railway.internal` the companion uses.
2. **Source**: Branch **`plus`**. Root Directory: leave **empty** (the build needs the whole repository).
3. **Build settings** come from `railway.json` at the repository root, which Railway reads
   automatically for this service: Dockerfile builder with `deploy/grocy/Dockerfile`, healthcheck
   `/login` (300 s), restart on failure. If the service shows a **Config-as-code → Railway Config File**
   field, enter **`/railway.json`**. Check under **Build** that Builder shows *Dockerfile* and Dockerfile
   Path `deploy/grocy/Dockerfile`. If a build log ever mentions *Railpack* or `composer install` failing,
   Railway didn't read the file: add the variable `RAILWAY_DOCKERFILE_PATH=deploy/grocy/Dockerfile` and
   redeploy. Optional, **Build → Watch Paths**: `/**` and `!/companion/**` so companion changes don't
   rebuild Grocy.
4. **Volume:** on the canvas, right-click empty space (or press ⌘K / Ctrl+K) → **Volume** → choose the
   `grocy` service → mount path **`/var/www/html/data`**. A small volume box appears attached to `grocy`.
5. **Variables** tab → **Raw Editor** → paste and **Update Variables**:
   ```
   PORT=8080
   GROCY_CURRENCY=EUR
   GROCY_DEFAULT_LOCALE=en
   GROCY_STOCK_BARCODE_LOOKUP_PLUGIN=OpenFoodFactsBarcodeLookupPlugin
   GROCY_FEATURE_FLAG_STOCK_PRODUCT_OPENED_TRACKING=true
   GROCY_CALENDAR_FIRST_DAY_OF_WEEK=1
   GROCY_MEAL_PLAN_FIRST_DAY_OF_WEEK=1
   TZ=Europe/Vilnius
   ```
   (Lithuanian exists as `lt`, ~93 % translated. Each person can pick a language in Grocy's user
   settings; set `GROCY_DEFAULT_LOCALE=lt` if Lithuanian should be the default. Optional backup
   settings: `GROCY_BACKUP_HOUR`, `GROCY_BACKUP_KEEP`, `GROCY_BACKUP_DAILY` — see *Backups*.)
6. **Settings → Networking → Public Networking → Generate Domain**. If asked for a port, enter
   **8080**. You get something like `grocy-production-ab12.up.railway.app` = `<grocy-domain>`.
7. Press **Deploy**. Open **Deployments** → the running build → **View logs**. The first build takes
   5–10 minutes (PHP extensions, JS and PHP packages). Success looks like:
   ```
   [grocy+] Seeding data volume from image
   [grocy+] Creating data/config.php from config-dist.php
   [grocy+] Requested / (HTTP 302), migrations done
   ```
   and the deployment turns green (*Active*) after the healthcheck passes.

### Step 3 — first login to Grocy
Open each link in a browser (desktop is easiest):
1. `https://<grocy-domain>/login` → user **admin**, password **admin**.
2. **Change the admin password now:** `https://<grocy-domain>/users` → pencil icon next to *admin* →
   enter a new password twice → **Save**. (Also reachable via the user menu, top right.)
3. **Raminta's user:** same page `https://<grocy-domain>/users` → **Add** → username, first name, password
   → **Save**. New users get all permissions by default (`GROCY_DEFAULT_PERMISSIONS` = ADMIN), which is
   what a shared household wants.
4. **Companion API key:** `https://<grocy-domain>/manageapikeys` → **Add** → description `companion` →
   OK. The new key is highlighted in the list; copy the long value in the *API key* column. This is
   `GROCY_API_KEY` for step 5.
5. **Phone keys:** optional now — the Grocy apps can log in with username/password and create their own
   key. If you prefer separate keys: log in as each person and repeat step 4 on their account.
6. **Language per person:** `https://<grocy-domain>/usersettings` → *Language*.
7. `https://<grocy-domain>/about` shows the Grocy version (4.7.1) — a quick check that all is well.

### Step 4 — turn on Railway backups for Grocy
Click the `grocy` service → **Backups** tab (appears once the volume exists):
1. **Edit schedule** (or *Backup schedules*) → tick **Daily** and **Weekly** (Monthly too if you like) →
   save. Daily copies are kept 6 days, weekly 1 month, monthly 3 months.
2. Press **Backup now** (or *Create backup*) once, so there's a copy of the fresh setup.
The built-in snapshots (before upgrades and daily at 03:00) run on their own; nothing to switch on.

### Step 5 — the `companion` service
1. On the canvas: **+ Create** (or ⌘K) → **GitHub Repo** → `brunopratus/grocy` again. A second box
   appears; open its **Settings**:
   * **Service name**: `companion`.
   * **Source**: Branch **`plus`**, **Root Directory: `/companion`**.
   * **Config-as-code → Railway Config File**: **`/companion/railway.json`** (absolute path; it does not
     follow the root directory). Sets Dockerfile build, healthcheck `/health`.
   * Optional, **Build → Watch Paths**: `/companion/**`, so Grocy-only changes don't rebuild the companion.
2. **Volume:** right-click canvas / ⌘K → **Volume** → service `companion` → mount path **`/data`**.
3. **Variables → Raw Editor**, paste, fill in the `<…>` parts, **Update Variables**:
   ```
   GROCY_URL=http://${{grocy.RAILWAY_PRIVATE_DOMAIN}}:8080
   GROCY_API_KEY=<key from step 3.4>
   ANTHROPIC_API_KEY=<sk-ant-… from console.anthropic.com>
   COMPANION_TOKEN=<your long random string>
   CLAUDE_MODEL=claude-sonnet-5-5
   CLAUDE_MODEL_CHEAP=claude-haiku-5-5
   USDA_FDC_API_KEY=<key from the USDA e-mail, or DEMO_KEY>
   NTFY_TOPIC_URL=https://ntfy.sh/<your-topic>
   TZ=Europe/Vilnius
   ```
   `${{grocy.RAILWAY_PRIVATE_DOMAIN}}` is a Railway *reference variable*: Railway fills in
   `grocy.railway.internal`, so the companion talks to Grocy over the private network. Projects created
   after 16 Oct 2025 support IPv4 and IPv6 there; if the companion log ever shows it can't reach Grocy,
   use the public address instead: `GROCY_URL=https://<grocy-domain>`.
   Optional: `HOUSEHOLD_PEOPLE=2`, `ALERT_HOUR=8`, `ALERT_DUE_DAYS=3`, `GENERIC_FOOD_SOURCES=fineli,usda`,
   `CLAUDE_FALLBACKS=true`.
4. **Settings → Networking → Generate Domain** (port **8000** if asked) = `<companion-domain>`.
5. **Deploy**. Build takes 1–2 minutes. In the logs look for
   `setup done: {'changes': [...], 'warnings': [], 'ok': True}` — the companion has created units,
   locations, stores, meal-plan sections and nutrition fields in Grocy.
   (Check in Grocy: `https://<grocy-domain>/userfields`, `/locations`, `/shoppinglocations`.)
6. Companion volume backups: `companion` → **Backups** → **Weekly** is enough.

### Step 6 — check everything
1. `https://<companion-domain>/health` → `{"ok":true}`.
2. `https://<companion-domain>/` → log in with `COMPANION_TOKEN` → the home screen with 8 buttons.
3. **Open / due** shows "Nothing opened or due" (empty stock — expected).
4. Send a test alert: on a computer run
   `curl -X POST -H "X-Companion-Token: <token>" "https://<companion-domain>/alerts/send?force=true"`
   → the ntfy app on both phones gets "Grocy+: nothing to use up".
5. Nutrition sources: `curl -H "X-Companion-Token: <token>" "https://<companion-domain>/nutrition/search?q=carrot&fi=porkkana"`
   → candidates from Fineli and USDA with kcal per 100 g.
6. Then set up the phones (see *Phone setup*).

### Updating later
Every push to `plus` redeploys automatically (only the service whose files changed, thanks to the watch
paths). Grocy upgrades arrive as weekly PRs — see *Branch and upstream sync model*. Services with a
volume have a few seconds of downtime on each deploy.

### Cost on Railway
The Hobby plan ($5/month, which includes $5 of usage) normally covers both services and volumes for a
household; Railway bills any usage above that. See **Workspace → Usage** in Railway.

## Getting the keys and secrets

| Variable | Where it comes from | Cost |
|---|---|---|
| `GROCY_API_KEY` | Your Grocy (step 3.4 above) | free |
| `ANTHROPIC_API_KEY` | Claude Console | pay per use |
| `USDA_FDC_API_KEY` | data.gov sign-up | free |
| `NTFY_TOPIC_URL` | You invent it | free |
| `COMPANION_TOKEN` | You generate it | free |

**Grocy API keys.** Log in to Grocy as the person the key is for → user menu (top right) →
**Manage API keys** → **Add**. Copy the long key that appears. Make one for the companion (logged in as
admin) and one per phone (logged in as that person), so each can be revoked alone. The phone apps can
also create their own key when you log in with username and password.

**Anthropic (Claude) API key.**
1. Go to <https://console.anthropic.com> and sign up/log in (this is a different account area from the
   Claude chat app, even with the same e-mail).
2. **Settings → Billing**: add a card and buy credits (start with $5–10; see *Costs*). Optionally turn on
   auto-reload with a low amount. Without credits the key exists but every request fails.
3. **Settings → Limits**: set a monthly spend limit (e.g. $10) so a bug can never run up a bill.
4. **API keys → Create key**, name it `grocy-plus`, copy it once (it's shown only once) into Railway as
   `ANTHROPIC_API_KEY`. Optionally create a separate workspace `grocy-plus` first and make the key
   there, so its usage and limit are separate from anything else.

**USDA FoodData Central key.** Open <https://fdc.nal.usda.gov/api-key-signup>, enter name and e-mail;
the key arrives by e-mail within minutes. Free, 1,000 requests/hour (far more than needed: it's only used
when a new market product is created). Fineli needs no key at all. `DEMO_KEY` works without signing up but is limited to about 30
requests/hour and 50/day per IP, which can fail on busy days.

**ntfy topic.** No account needed. Invent a long, random topic name, e.g. run `openssl rand -hex 8` or
type random letters: `grocyplus-7f3a9c2e51d4`. Then `NTFY_TOPIC_URL=https://ntfy.sh/grocyplus-7f3a9c2e51d4`,
and subscribe to exactly that topic in the ntfy app on both phones (server `ntfy.sh`). Anyone who knows
the name can read or send messages, so don't make it guessable. Test it before deploying:
`curl -d "Hello from Grocy+" https://ntfy.sh/<your-topic>`.

**COMPANION_TOKEN.** A long random string that you and Raminta type once on the companion's login
page (the phone then remembers it for a year). Generate with `openssl rand -hex 24`, or use a password
manager's generator (32+ characters). Changing it logs both phones out.

## Healthchecks

After each deploy, Railway calls the healthcheck path and only switches traffic to the new version once
it answers **HTTP 200** (old version stays live otherwise, so a broken build never replaces a working one).
It is a deploy-time check, not ongoing monitoring.

* `grocy`: `/login`, timeout 300 s. The Grocy login page needs no API key and only answers once Apache,
  PHP and the database work. (`/api/...` can't be used: it returns 401 without a key.)
* `companion`: `/health`, timeout 120 s; returns `{"ok": true}` without a token.

## Phone setup

**ntfy (both phones):** install *ntfy* (App Store / Google Play) → subscribe to the topic from
`NTFY_TOPIC_URL` (server `ntfy.sh`). The alert arrives daily at 08:00 Vilnius time with opened items
and items due within 3 days, e.g. `🟠 Milk — opened, 600 ml left, 1 day left`.
The topic name is the only secret on public ntfy.sh, so keep it long and random (or self-host ntfy).

**iPhone (Grocy Mobile):** open the app → add server → URL `https://<grocy domain>` → paste the
iPhone's API key. Then in Safari open `https://<companion domain>`, log in with `COMPANION_TOKEN`,
Share → **Add to Home Screen**. Camera barcode scanning in the companion needs HTTPS (Railway
domains are HTTPS).

**Android (Grocy Android, Raminta):** install *Grocy Android* (Google Play/F-Droid) → server URL
`https://<grocy domain>` → API key (or log in with her Grocy user to create one). In Chrome open the
companion URL, log in, menu → **Install app / Add to Home screen**.

### Everyday use
* **Shopping:** scan in the Grocy apps as usual, or in the companion (*Scan barcode*) to also get
  nutrition, then *Add to stock* with the price and store. Unknown barcodes ask for a photo of the
  nutrition table.
* **Market (Turgus):** *Market produce* → "morkos, 850 g, €1.20". First time, the product is created
  with nutrition from Fineli or USDA and typical shelf life.
* **Cooking:** *Cook recipe* consumes the ingredients and marks partly used packs as opened, so the
  due date follows "use within N days after opening". The notice tells you what's left and until when.
* **Jars and leftovers:** *Weigh item* sets the real remaining amount (enable tare weight in Grocy for
  jars you weigh including the jar).
* **Sunday:** *Week plan* → upload the PDF you received → preview (dry run) → generate for real.
  Recipes, meal plan and shopping list appear in the Grocy apps.
* **Weekly report:** cost and nutrition per day and per person, what was used and what was thrown
  away (consume with "spoiled" in the Grocy apps).

## Branch and upstream sync model

* `release` — untouched mirror of `grocy/grocy@release` (stable). Never commit here.
* `master` — upstream's development branch, not used.
* `plus` — default branch: `release` + Grocy+ files. Railway deploys from it.

`.github/workflows/upstream-sync.yml` runs every Monday 06:00 UTC (and on demand: Actions →
*Upstream sync* → Run workflow):
1. fast-forwards `release` to `upstream/release` (fails loudly if `release` was ever modified);
2. if `plus` is missing upstream commits, creates `sync/upstream-<date>`, merges `upstream/release`
   into it and opens a PR into `plus` titled with the new Grocy version, listing the commits. If the
   merge conflicts (only possible if upstream adds a path Grocy+ also uses), it opens a **draft** PR
   with the conflicting files.

PRs opened by `GITHUB_TOKEN` don't trigger other workflows; that's expected. If upstream ever ships
`.github/workflows/*` files, `GITHUB_TOKEN` can't push them: add a fine-grained PAT as repository
secret `SYNC_TOKEN` (contents, pull requests, workflows: read/write) and the workflow uses it.

### Upgrading Grocy
1. Optional extra safety: Railway → `grocy` → Backups → **Backup now**.
2. Merge the sync PR into `plus`.
3. Railway rebuilds the `grocy` service. Before Apache starts, the entrypoint notices the new Grocy
   version and writes a full snapshot (`backups/grocy-…-pre-upgrade-<old>-to-<new>.db.gz` plus
   `files-….tgz`), then requests `/`, which runs Grocy's database migrations. Check the deploy log
   for `snapshot before migrations` and `migrations done`.
4. Read the upstream changelog in the PR for breaking API changes; the companion's CI runs on its
   own changes only, so open the companion once after an upgrade (or run `POST /setup`).

## Costs

* **Railway:** two small services and two small volumes; typically a few USD/month on the Hobby plan.
* **Claude API: separate, pay-as-you-go.** A Claude.ai Pro/Max subscription does **not** include API
  usage; the API is billed through console.anthropic.com from prepaid credits you buy with a card
  (each call deducts its cost; auto-reload and a monthly spend limit are optional). Prices per million
  tokens: Sonnet 5.5 $2 in / $10 out, Haiku 5.5 $0.10 / $0.50.
  * label photo or due-date photo (Sonnet, images downscaled to 1568 px): typically about **1 cent**;
  * new market produce (2 Haiku calls) or new barcode product (1 Haiku call): **< 0.1 cent**;
  * weekly meal plan from a PDF (Sonnet, large input and output): roughly **10–30 cents**;
  * receipt photo: about 1–2 cents.
  A normal month is usually **$1–3**. Exact numbers: `GET /usage` (tokens and estimated cost per
  task, logged per call), and the Console's Usage page.
* Open Food Facts, Fineli, USDA FDC and ntfy.sh are free (Fineli needs no key).

## Backups

Two layers protect the Grocy volume (`/var/www/html/data`: database, photos, config):

**1. Railway volume backups (protect against losing the volume).** Railway → `grocy` service →
**Backups** tab:
* turn on the **Daily** schedule (kept 6 days) and **Weekly** (kept 1 month); Monthly (kept 3 months)
  is cheap insurance too;
* **Backup now** creates a manual one at any time (do one right after the first setup);
* **Restore** stages a new volume from the backup, mounted where the old one was; review and deploy
  the staged change. The old volume is kept, unmounted.
* Backups belong to the volume: deleting ("wiping") the volume deletes its backups, so download a
  copy now and then (below).

**2. Snapshots inside the volume (protect against bad upgrades and mistakes).** Built into the image:
* before every Grocy version change: full snapshot (database + `storage/` + `config.php`);
* every day at 03:00 (`TZ`): database snapshot;
* on demand: `railway ssh --service grocy` then `grocy-backup` (database) or
  `grocy-backup --full` (with photos).

Files go to `data/backups/` as `grocy-<date>-<label>.db.gz` (made with SQLite `VACUUM INTO`, which is
consistent while Grocy is running) and `files-<date>-<label>.tgz`; the newest 14 of each are kept.
Settings: `GROCY_BACKUP_DAILY=false` to turn the daily one off, `GROCY_BACKUP_HOUR=03`,
`GROCY_BACKUP_KEEP=14`.

**Off-site copy** (e.g. monthly, to your computer) with the [Railway CLI](https://docs.railway.com/guides/cli).
I couldn't test the CLI from the build environment, so check the first copy opens (e.g. in
[DB Browser for SQLite](https://sqlitebrowser.org/)):
```bash
railway link                       # pick the grocy-plus project
railway ssh --service grocy        # opens a shell in the container, then:
grocy-backup monthly
base64 -w0 "$(ls -t /var/www/html/data/backups/grocy-*.db.gz | head -1)"; echo
# copy the printed text into a file grocy.b64 on your computer, then exit and run:
#   base64 -d grocy.b64 > grocy.db.gz && gunzip grocy.db.gz
```

**Restore a snapshot** (stops writes for a moment):
```bash
railway ssh --service grocy
cd /var/www/html/data && ls -t backups/
gunzip -c backups/grocy-<date>-<label>.db.gz > grocy.db.restore
mv grocy.db grocy.db.before-restore && mv grocy.db.restore grocy.db && chown www-data: grocy.db
# for a --full snapshot also: tar -xzf backups/files-<date>-<label>.tgz -C /var/www/html/data
exit
```
Then restart the service in Railway.

**Companion volume** (`/data`): history, Claude cost log, generated plans, receipt originals. Losing it
loses only history (Grocy holds all household data); a weekly Railway backup is enough.

## Nutrition data sources

| Source | Used for | Notes |
|---|---|---|
| Open Food Facts | barcodes | Free, open. Coverage of Lithuanian-only products (barcodes starting `477`) is thin, so expect label photos for many local brands; international brands sold at Lidl/Maxima are usually present |
| Label photo + Claude | anything with a nutrition table | Reads LT/LV/ET/PL/EN labels; the authoritative source for packaged food |
| **Fineli** (Finnish Institute for Health and Welfare, THL) | market produce, generic foods | Open data, CC BY 4.0; Nordic foods close to Lithuanian ones, per 100 g. Searched first with the Finnish name. Products sourced from it get `nutrition_source=fineli` and the attribution "Fineli (THL), CC BY 4.0 #<id>" in their description. Grocy+ is not produced or endorsed by THL |
| USDA FoodData Central | market produce, generic foods | Free API, Foundation/SR Legacy per 100 g. Searched in parallel with Fineli; Claude (Haiku) picks the best match across both, preferring Fineli when equally good |
| Lithuanian national food composition database (SMLPC, *Maisto produktų sudėties ir energijos duomenų bazė*) | reference only (see phase 6) | Official generic-food tables, linked from the Ministry of Health; web lookup only, no public API found |
| Retailer sites (Barbora/Maxima, Rimi e-shops) | – | Show nutrition on product pages but have no public API; scraping may break their terms, so not used |

`GENERIC_FOOD_SOURCES` (default `fineli,usda`) chooses which generic sources are searched.
`GET /nutrition/search?q=carrot&fi=porkkana` shows what each source returns — useful to check values.

## Roadmap

* **Phase 4 — receipts:** `POST /receipts/photo` already parses a receipt photo with Claude, maps
  lines to products and (with `commit=true`) adds stock with prices and store, keeping the original.
  Next: an IMAP mailbox for Maxima/Rimi e-receipts, remembering line → product mappings, and an
  optional (unofficial, fragile) Lidl Plus connector. Interfaces in `companion/app/receipts.py`.
* **Phase 5 — bank link:** match grocery card transactions from the bank app
  (`brunopratus/bank`, also on Railway) to receipts by date, amount and merchant; spending per
  week/store next to nutrition targets. Interface only: `companion/app/bank.py`, `GET /bank/matches`
  returns 501.
* **Phase 6 — a Lithuanian food database (future, not started).** Build an open database of the foods
  actually sold and eaten in Lithuania, which today exists only partly (national generic tables without an
  API, few Lithuanian barcodes in Open Food Facts). Ideas to keep in mind:
  * **Seed data already being collected:** every label photo the household reads is stored in the
    companion DB (`events` with `kind='label'`): original Lithuanian product name, brand, barcode, net
    quantity, per-100 values, label language, shelf life after opening. Barcode lookups and produce
    matches are stored too. Nothing has to change in daily use to start building it.
  * **Generic foods:** Lithuanian names (*morkos, grikiai, varškė, kefyras, rūgpienis, juoda ruginė
    duona*…) mapped to Fineli/USDA entries, with values checked against the national SMLPC tables;
    traditional dishes (cepelinai, šaltibarščiai, kibinai) as recipes built from those.
  * **Packaged products:** `477…` barcodes from label photos, with the label photo kept as evidence.
  * **Shape:** its own service or package (`lt-foods`) with an export (CSV/JSON) and a small API that the
    companion queries before Open Food Facts/Fineli/USDA.
  * **Sharing back:** contribute packaged products to Open Food Facts (ODbL licence, photos required —
    which the label workflow already has); check the licence of anything derived from the SMLPC tables
    before publishing it.
  * **Quality:** a review step (household confirms Claude's reading), duplicate detection per barcode,
    and recording the source and date of every value.

## Local development

```bash
# Grocy
docker build -f deploy/grocy/Dockerfile -t grocy-plus .
docker run -d --name grocy -p 8080:8080 -v grocy-data:/var/www/html/data grocy-plus
# create an API key in the UI (admin/admin), or for tests:
#   INSERT INTO api_keys (api_key, user_id, expires, key_type) VALUES ('dev', 1, '2999-12-31 23:59:59', 'default');

# Companion
cd companion && pip install -r requirements-dev.txt
GROCY_URL=http://localhost:8080 GROCY_API_KEY=dev COMPANION_TOKEN=dev COMPANION_DB_PATH=./dev.db \
  ANTHROPIC_API_KEY=... uvicorn app.main:app --reload --port 8000
pytest -q
```
