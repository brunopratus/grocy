# Grocy+ companion

FastAPI service that sits next to Grocy and adds what Grocy lacks: nutrition capture,
waste-aware cooking, weekly meal planning with Claude, reports and a daily "use it up" alert.
The phone UI is served at `/`. Project overview and Railway setup: [`../GROCY_PLUS.md`](../GROCY_PLUS.md).

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GROCY_URL` | yes | `http://localhost:8080` | Grocy base URL (private: `http://grocy.railway.internal:8080`) |
| `GROCY_API_KEY` | yes | | API key created in Grocy for the companion |
| `ANTHROPIC_API_KEY` | yes | | Claude API key |
| `COMPANION_TOKEN` | yes | | Shared secret for the API and the phone UI login. Without it every call returns 503 |
| `CLAUDE_MODEL` | | `claude-sonnet-5-5` | Label photos, due dates, meal plans, receipts |
| `CLAUDE_MODEL_CHEAP` | | `claude-haiku-5-5` | Translation, storage advice, USDA match picking |
| `CLAUDE_FALLBACKS` | | `true` | Server-side refusal fallback (`fallbacks: "default"`) for non-Haiku calls |
| `USDA_FDC_API_KEY` | | `DEMO_KEY` | Free key from <https://fdc.nal.usda.gov/api-key-signup> (DEMO_KEY is rate limited) |
| `NTFY_TOPIC_URL` | | | e.g. `https://ntfy.sh/grocyplus-<random>`; daily alert is skipped when empty |
| `TZ` | | `Europe/Vilnius` | Scheduler and dates |
| `ALERT_HOUR` / `ALERT_DUE_DAYS` | | `8` / `3` | Daily alert time and "due within" window |
| `HOUSEHOLD_PEOPLE` | | `2` | Per-person numbers in reports and plans |
| `COMPANION_DB_PATH` | | `/data/companion.db` | SQLite file (events, Claude cost log, plans); keep `/data` on a volume |

## Auth

Every endpoint except `/`, `/health`, `/login`, static files and the manifest needs the token, as
`X-Companion-Token: <token>`, `Authorization: Bearer <token>`, or the `companion_token` cookie set by
`POST /login {"token": "..."}` (HttpOnly, SameSite=Strict, one year).

```bash
C=https://companion.example.up.railway.app
T="X-Companion-Token: $COMPANION_TOKEN"
```

## Endpoints

### Setup
| Method | Path | |
|---|---|---|
| POST | `/setup` | Idempotent bootstrap (also runs at startup): quantity units g/kg/ml/l/Piece/Pack with kg→g and l→ml conversions, locations Fridge/Freezer/Pantry, stores Maxima/Rimi/Lidl/Norfa/IKI/Turgus, meal plan sections, product userfields (`kcal_100` … `salt_100`, `nutrition_source`, `net_amount_per_pack`, `net_unit`, `label_photo`), recipe userfield `grocy_plus_week`, and `treat_opened_as_out_of_stock=0` on food products. Returns `{changes, warnings, ok}` |
| GET | `/lookups` | Products, recipes, stores, locations for the UI pickers |
| GET | `/health` | Liveness (no auth) |

### Product capture
| Method | Path | Body | |
|---|---|---|---|
| POST | `/products/barcode/{code}` | – | Existing Grocy product, else Open Food Facts → new product with nutrition userfields, pack-size conversion, barcode and picture. `status`: `existing` / `created` / `not_found` / `lookup_failed`; `needs_label_photo: true` means "photo of the nutrition table needed" |
| POST | `/products/{id}/label-photo` | multipart `images` (1–2 files: nutrition table, then front/net weight), optional `barcode` | Claude reads name, brand, net quantity, nutrition per 100 g/ml (LT/LV/ET/PL/EN; per-portion-only labels are scaled), suggested storage location and shelf life after opening. Use `new` as `{id}` to create the product. The table photo is stored in the `label_photo` userfield, the front photo becomes the product picture |
| POST | `/products/produce` | `{name, weight_g? , count?, price_eur?, store? = "Turgus", best_before?}` | Market produce without barcode. New products: Claude (cheap model) translates to a generic English food term and gives storage values, USDA FDC (Foundation/SR Legacy first) gives per-100 g nutrition, Claude picks the best match. Stock unit is g; `count` uses the typical piece weight. Adds stock with price and store |
| POST | `/stock/add` | `{product_id or barcode, amount, unit?, price_total?, store?, best_before?, location?}` | Converts to the stock unit (pack/piece via the pack size) and books it with unit price and shopping location, so Grocy's per-store price history works |
| POST | `/ocr/due-date` | multipart `image` | `{date: "YYYY-MM-DD" | null, kind, raw_text, confidence}` |

### Cooking and partial use
| Method | Path | Body | |
|---|---|---|---|
| POST | `/recipes/{id}/cook` | `{servings?}` | Snapshots ingredient stock entries, consumes the recipe in Grocy, then opens every entry that shrank but still has stock and wasn't open (`/stock/products/{pid}/open` with that `stock_entry_id`), so Grocy recalculates the due date from `default_best_before_days_after_open`. Returns notices like `Spaghetti: opened 2026-10-18 for 'Lentil bolognese', 500 g left, use by 2026-11-17`, plus cost and kcal/macros (total and per serving) of what was actually consumed. Tare-weight and "can't be opened" products get a notice instead |
| POST | `/stock/{product_id}/weigh` | `{grams_measured, includes_container?, container_g?}` | Sets the real remaining amount with Grocy inventory (product-wide). With tare handling on, the weight includes the container by default; otherwise give `container_g` when it does. Logs the difference |
| GET | `/stock/open-due?days=3` | – | Opened items and items due within N days |
| GET | `/events?limit=100&kind=` | – | History: `added`, `created`, `label`, `opened`, `used`, `weighed`, `cooked`, `plan`, `alert` |

### Reports
| Method | Path | |
|---|---|---|
| GET | `/recipes/{id}/nutrition` | Ingredients × per-100 userfields → total and per serving, plus cost from Grocy fulfillment |
| GET | `/reports/week?start=YYYY-MM-DD` | Meal plan cost and nutrition per day and per person, stock consumed, wasted (spoiled consumes) and inventory corrections. Default: this week's Monday |
| GET | `/usage?since=YYYY-MM-DD` | Claude tokens and estimated USD cost per task/model |

### Meal plan
`POST /mealplan/generate` (multipart)

| Field | |
|---|---|
| `file` | The weekly plan the household received (PDF, text or photo) |
| `text` | …or the plan as text |
| `week_start` | Default: next Monday |
| `people`, `kcal_target`, `budget_eur`, `preferences` | Optional |
| `dry_run` | `true` returns the plan without writing |

Reads Grocy stock (amounts, due dates, opened) and volatile stock, asks Claude for an adapted plan
(opened and soon-due items first, stock before buying, the received plan's structure kept), then
creates recipes + ingredients (new products only when needed), meal plan entries in the
Breakfast/Lunch/Dinner sections, and adds missing ingredients to the shopping list through Grocy's
per-week meal plan recipe. Generated recipes carry `grocy_plus_week=<week_start>`; re-running the
same week replaces only those. The user's own recipes are never deleted.

Response: `summary.days[].meals[]` with `uses_waste_risk`, `cost_eur`, `nutrition_per_person`,
day totals, `week_cost_eur`, `shopping_delta` (Claude's list) and `shopping_delta_grocy` (what was
actually added).

### Alerts
| Method | Path | |
|---|---|---|
| GET | `/alerts/preview` | Same content as the daily message |
| POST | `/alerts/send?force=false` | Send now. The scheduler sends daily at `ALERT_HOUR`:00 `TZ` (skipped if nothing to report) |

### Later phases
| Method | Path | |
|---|---|---|
| POST | `/receipts/photo` | multipart `image`, `commit` (default false). Phase 4 early version: Claude parses a receipt photo, maps lines to products; with `commit=true` matched food lines are added to stock with price and store. Originals kept in `/data/receipts`. IMAP e-receipts and Lidl Plus are TODO (`app/receipts.py`) |
| GET | `/bank/matches` | Phase 5, returns 501. Interface in `app/bank.py` |

## Claude usage

All calls go through `app/claude.py`: images and PDFs as base64 blocks, one tool whose
`input_schema` is the Pydantic model's JSON schema, Pydantic validation with one retry (the
validation error is sent back as a `tool_result`), and a row per call in `claude_calls` with tokens
and estimated cost. `tool_choice` is `auto` with an explicit instruction, because Claude Sonnet 5.5
returns 400 for forced tool use; a missing tool call counts as a failed attempt. Only the data a
task needs is sent (product names, stock amounts, photos); the Grocy API key never is.

## Development

```bash
cd companion
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app tests && ruff format --check app tests && mypy app && pytest -q
GROCY_URL=http://localhost:8080 GROCY_API_KEY=... COMPANION_TOKEN=dev COMPANION_DB_PATH=./dev.db \
  uvicorn app.main:app --reload --port 8000
```

Tests mock Grocy (respx at HTTP level, plus an in-memory fake) and Claude; they cover unit
conversion, the cook/open logic, nutrition math, the Claude wrapper, weighing and plan→Grocy mapping.

| Module | |
|---|---|
| `config.py`, `db.py`, `deps.py` | Settings, SQLite store, service container |
| `grocy.py`, `claude.py` | API clients |
| `units.py`, `nutrition.py` | Conversions, per-100 math, Open Food Facts, USDA |
| `setup.py` | Bootstrap and the ID catalog |
| `products.py`, `stock.py` | Capture; cooking, weighing |
| `mealplan.py`, `reports.py`, `alerts.py` | Planning, reporting, notifications |
| `receipts.py`, `bank.py` | Phase 4 / 5 |
| `routes.py`, `main.py` | HTTP API, auth, scheduler |
