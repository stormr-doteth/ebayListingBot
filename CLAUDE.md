# RetroList — eBay + Mercari Game Lister
> Claude Code context file. Read this before making any changes.

## What This Is
A Flask web app that lets Storm photograph retro video games, identifies and grades them with Claude, prices them at the PriceCharting loose or CIB value, writes an honest eBay listing, and publishes it via the eBay Inventory API — and optionally cross-lists it on Mercari (browser automation), ending the other listing when it sells on either. Built to be used from a phone browser while the server runs on a Mac.

## Project Structure
```
ebayListingBot/
├── app.py                           # Flask backend — all routes, Claude pipeline, eBay publishing, sale sync
├── crosslist.py                     # Inventory DB (SQLite) + Mercari publisher (Playwright worker)
├── templates/
│   ├── index.html                   # Single-page frontend (light/dark)
│   └── login.html                   # PIN login (only used when APP_PIN is set)
├── pricecharting_master_price.csv   # Local price guide (~28k games, 17 consoles, from the PriceCharting scraper)
├── setup_policies.py                # One-time: create eBay business policies
├── setup_shipping_policy.py         # One-time: create calculated-shipping policy
├── requirements.txt
├── .env                             # Secrets/config (gitignored)
├── ebay_tokens.json                 # Saved eBay OAuth tokens (gitignored)
├── ebay_config.json                 # eBay App ID / Cert ID / RuName from the setup screen (gitignored)
├── .flask_secret                    # Session key (gitignored, auto-created)
├── inventory.db                     # Every published item + its listing on each marketplace (gitignored)
├── mercari_profile/                 # Browser profile holding the Mercari login (gitignored)
└── uploads/                         # Photos + mercari_screens/, auto-deleted after UPLOAD_RETENTION_DAYS (gitignored)
```

## Stack
- **Backend**: Python 3.13, Flask 3.x (runs in `venv/`), Pillow + pillow-heif for photos
- **AI**: the local **Claude Code CLI** in headless mode (`claude -p`). It runs on Storm's Claude plan login — no API key, no per-call billing. `run_claude()` removes `ANTHROPIC_API_KEY` from the subprocess env so it can never silently bill an API key.
- **Pricing**: PriceCharting loose/CIB price — local `pricecharting_master_price.csv` snapshot, or the live PriceCharting API when `PRICECHARTING_API_KEY` is set. No web research.
- **Listing**: eBay Inventory REST API + Trading API `UploadSiteHostedPictures` for images; Mercari via Playwright (no public API)
- **Frontend**: Vanilla HTML/CSS/JS, no framework, Inter font

## Environment Variables (in `.env`)
```bash
# eBay (required to publish)
EBAY_TOKEN=v^1.1-...                   # Legacy static token (expires in ~2h) — not needed once OAuth is connected
# App ID / Cert ID / RuName are normally entered in the app ("⚙ Set up eBay", saved to ebay_config.json).
# EBAY_APP_ID / EBAY_CERT_ID / EBAY_REDIRECT_URI in .env still work as a fallback.
EBAY_SANDBOX=false
EBAY_FULFILLMENT_POLICY=255223329024
EBAY_PAYMENT_POLICY=255223242024
EBAY_RETURN_POLICY=255223411024
EBAY_LOCATION_KEY=RETRO_HQ             # Inventory location key (default RETRO_HQ)

# App (all optional)
APP_PIN=                               # Strongly recommended — without it anyone on the network can publish
HOST=0.0.0.0  PORT=8080
PRICECHARTING_API_KEY=                 # paid PriceCharting API → live prices instead of the CSV snapshot
UPLOAD_RETENTION_DAYS=14

# Claude (all optional)
CLAUDE_MODEL=sonnet                    # or opus for harder IDs (slower, uses more plan quota)
ANALYZE_WORKERS=3                      # games analyzed in parallel
CLAUDE_TIMEOUT=240
CLAUDE_BIN=                            # path to `claude` if not on PATH

# Cross-listing (all optional)
MERCARI_DRY_RUN=true                   # TEST MODE: fill Mercari's form + screenshot, never click List. Set false to go live
MERCARI_PRICE_MARKUP=0                 # % added to the eBay price on Mercari (e.g. 5); Mercari takes whole dollars
MERCARI_MIN_GAP=90                     # seconds between two live Mercari listings (human pace)
MERCARI_BROWSER=chrome                 # "chrome" = installed Google Chrome; empty = Playwright's Chromium
MERCARI_HEADLESS=false                 # real window is less likely to get bot-checked...
MERCARI_OFFSCREEN=true                 # ...but it's parked off-screen (login window is always on screen)
CROSSLIST_SYNC_MINUTES=10              # how often to check both marketplaces for sales (0 = off)
CROSSLIST_AUTO_END=true                # end the other listing automatically when an item sells
```
`GEMINI_API_KEY` / `CEREBRAS_API_KEY` are no longer used.

## Claude Pipeline (app.py)
Per game, run in a thread pool and streamed to the browser as SSE (`/analyze-batch`):
1. **`save_photos()`** — every upload is re-encoded to JPEG (HEIC converted, rotation fixed, EXIF/GPS stripped), max 2400px for eBay, plus a 1568px copy in `ai/` for Claude.
2. **`analyze_photos()`** — ONE `claude -p` call with the photos attached via `@photo_N.jpg` mentions (the CLI only attaches ~3, so extras go on a numbered contact sheet and Claude may `Read` a full-size photo). Returns `GAME_INFO_SCHEMA`: identification (title, platform, variant, region, MPN, completeness), condition, authenticity, confidence, flags, **plus the eBay title + HTML description** and a rough `estimated_loose/cib` fallback.
3. **`price_listing()`** — pricing is deterministic, not Claude's call: PriceCharting API rows if `PRICECHARTING_API_KEY` is set, else the local CSV. `pick_price_row()` chooses the row for the exact variant (uses the variant field, listing title and MPN suffix like `GH`). Game + box + manual → **CIB** price; anything less → **loose**. Not in the guide → Claude's estimate, flagged.
4. **`_clean_game_info()` / `_clean_title()`** — guard rails: plain item-specific values, 80-char title, strip "Authentic" unless authenticity is `likely_authentic`.

**Photo dump** (`/group-photos`): up to 60 photos of many games at once. They're saved once, sorted by EXIF capture
time (fallback: file lastModified, then upload order) and renumbered, then ONE `claude -p` call (effort `medium` —
`low` mis-grouped a back photo in testing) sees numbered contact sheets (20 per sheet, ≤3 sheets) and returns groups
(`group_photos()`, repaired so every photo lands in exactly one group). The browser shows the groups in the batch
queue (✂ split / ⤒ merge) and `/analyze-batch` then receives `game_{n}_refs` instead of re-uploading files —
`_link_photos()` hard-links each group's photos into its own game folder. ~30–40s and ~30k tokens for 20 photos.

The UI shows which PriceCharting row was used (linked) and the other candidate rows with one-tap Loose/CIB price buttons.

Claude CLI safety: `--tools Read` only, and Read is *not* pre-approved so it only works inside the game's photo folder; `--setting-sources ""` ignores personal settings/hooks; `--no-session-persistence` keeps these runs out of session history. Photo text is treated as data (system prompt says so).

Typical cost: ~20–40k tokens and ~15–25s per game with 2–4 photos (more photos → more). Token usage is logged per game and shown in the AI check panel. Games run 3 at a time.

## Cross-listing (crosslist.py + app.py)
- **Inventory** (`inventory.db`): `items` (one per physical game, status active/sold) and `listings` (one per item ×
  marketplace: status queued/listed/draft/failed/sold/ended/end_failed, eBay `sku` + `offer_id`, Mercari item id `m…`).
  Only games published after this feature exists are tracked.
- **`publish_item()`**: eBay publishes synchronously as before; Mercari is queued to the Mercari worker and the UI polls
  `/items/status`. The browser sends back `item_id` on retries, and markets already listed/queued are skipped — so a
  retry never double-lists.
- **Mercari worker**: ONE thread owns a Playwright persistent context (`mercari_profile/`) — Playwright's sync API isn't
  thread-safe, so every Mercari action is a job on its queue. Browser starts on demand, closes after 60 s idle.
  Log in once via "Log in to Mercari" (opens a window on the Mac for 5 min) or `./venv/bin/python crosslist.py login`.
  The login is saved to `mercari_profile/login_state.json` (cookies + localStorage) after every job and restored
  on launch — the persistent profile alone drops session cookies on close, which logged Mercari out. All selectors are in `MERCARI_UI`;
  each is a list of fallbacks. Missing optional steps (brand, category) become warnings stored in `listings.error`
  and shown under the badge; missing required ones (photos, title, description, price) fail the job with a screenshot.
- **Mapping** (`to_mercari()`): HTML description → plain text + Platform/Region/Part number/Year lines (Mercari has no
  item specifics), ≤80-char title, ≤1000-char description, ≤12 photos, conditions NEW→New, LIKE_NEW/EXCELLENT→Like new,
  VERY_GOOD/GOOD→Good, ACCEPTABLE→Fair, brand from platform (Nintendo/Sony/…), category Electronics › Video Games & Consoles › Video Games.
  Shipping is left at Mercari's default — check it in a test-mode screenshot.
- **Sale sync** (`sync_sales()`, every CROSSLIST_SYNC_MINUTES + "Check for sales now"): eBay sales from the Fulfillment
  API `getOrders` (matched by SKU); Mercari sales by reading item ids off the in-progress/complete pages. A sale →
  item marked sold → other listings ended (eBay: `POST /offer/{id}/withdraw`; Mercari: item → Edit → Deactivate).
  If ending fails it's `end_failed` and a red banner in the UI says to end it by hand.
- **Photos**: local uploads if still there, else the permanent `ebayimg.com` URLs saved at eBay publish (`items.data.image_urls`)
  are downloaded — so cross-listing works after the upload cleanup and for imported listings.
- **Import eBay listings** (Inventory → button, `import_ebay_listings()`): live Inventory-API offers (everything RetroList
  listed) become inventory items, protected by the sale sync and cross-listable with “+ Mercari”. Seller Hub / eBay-app
  listings aren't visible to that API.
- **Failed ends** (`end_failed`) are retried every sync. If one sells before it's ended → `double_sold` + a 🚨 banner:
  cancel one of the two orders.
- **CLI** (Terminal on the Mac): `./venv/bin/python crosslist.py login | check | probe | sold`. `probe` dumps the real
  sell form's fields to `uploads/mercari_screens/probe.json` (+ `probe.png`) — use it to fix `MERCARI_UI`.
- `crosslist.py` loads `.env` itself: app.py imports it before its own `load_dotenv()`.
- Test mode was verified against a local mock of the sell form only — the real Mercari page needs a first dry run.
- History: a parallel implementation (branch `crosslist-mercari`, `inventory.py`/`mercari.py`) was superseded by this one;
  its login fix, eBay import, photo fallback, pacing, delist retry and double-sale alert were ported here.

## Routes
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Main UI |
| `/login` | GET/POST | PIN login (rate-limited: 5 tries / 5 min per IP) |
| `/analyze-batch` | POST | FormData `game_count`, `game_{n}_photos`, `game_{n}_notes`, `tested` → SSE `progress` / `result` / `error` / `done`. Used for single games too (count = 1). |
| `/group-photos` | POST | Photo dump: `photos` + `modified` (JSON lastModified list) → `{groups: [{item, photos: [{ref, index}]}]}` |
| `/publish` | POST | JSON `{listing, game_info, photos, markets: ["ebay","mercari"], item_id?}` → `{success, item_id, results: {market: {status, url, error}}, listing_id, url}` |
| `/publish-batch` | POST | JSON `{games: [{…, item_id?}], markets}` → `{results: [...]}` |
| `/items/status` | POST | `{ids}` → items with per-market listing status (polled while Mercari works) |
| `/inventory` | GET | Recent items, listings needing attention, sync state |
| `/sync/run` | POST | Check for sales now, end other listings, retry failed ends |
| `/inventory/import-ebay` | POST | Import live RetroList eBay listings into the inventory |
| `/items/<id>/list/<market>` | POST | Cross-list an existing inventory item (e.g. imported) |
| `/mercari/status`, `/mercari/login` | GET/POST | Login state (`?check=1` actually checks) / open the login window on the Mac |
| `/mercari/screenshot/<id>.png` | GET | Filled-form screenshot (test mode or failure) |
| `/ebay/auth`, `/ebay/callback` | GET | OAuth consent flow (with `state` check) |
| `/ebay/config` | GET/POST | Read (secret redacted) / save app credentials; verified with eBay before saving |
| `/ebay/code` | POST | Finish OAuth by pasting the redirect URL (eBay only redirects to https, the app is local http) |
| `/ebay/status`, `/ebay/disconnect` | GET/POST | Connection status (incl. `days_left` on the 18-month login, live check of a static token) / forget tokens |

**eBay login lifecycle:** set up once → Connect eBay → approve → paste the example.com link → the app
auto-refreshes the 2-hour access token from the ~18-month refresh token. A banner appears 30 days before it expires.

`photos` are refs like `<batch>/game_0/photo_0.jpg`, resolved by `resolve_photo()` which refuses anything outside `uploads/`. The browser never sends file paths.

## eBay API Flow
1. `POST /ws/api.dll` (Trading API `UploadSiteHostedPictures`) — upload images in parallel, get `ebayimg.com` URLs
2. `PUT /sell/inventory/v1/inventory_item/{sku}` — product, aspects, images, condition + `conditionDescription`
3. `POST /sell/inventory/v1/offer` — price, policies, category `139973` (Video Games)
4. `POST /sell/inventory/v1/offer/{offerId}/publish`

**Known eBay quirks:**
- Error 25001 / 5xx on inventory is transient — retried once
- Error 25002 ("Add at least 1 photo") means image upload failed — check token scopes
- `packageType` causes error 25101 for video games — intentionally omitted, only weight is sent
- SKUs are `RL-<SLUG>-<8 hex>` (letters, digits, hyphens; ≤50 chars)
- Aspects with unknown values (`N/A`, `Unknown`) are omitted rather than sent

## Frontend Behavior
- Mobile-first: phone camera → listing. All photos are analyzed; first photo leads the eBay gallery.
- Option: "Tested & working" (controls whether the listing may say Tested) — remembered in localStorage.
- Single game and batch both stream progress from `/analyze-batch`.
- "AI check" panel per game: ID confidence, authenticity, variant, flags, the PriceCharting row used (linked), alternative rows with one-tap prices, token usage.
- Everything editable before publishing (title, description, condition, weight, price, item specifics). Published batch cards link to the live listing and can't be published twice.
- All model text is HTML-escaped (`esc()`) before rendering.

## Design System
- Clean modern look, light + dark (follows `prefers-color-scheme`). Colors are CSS variables on `:root` (e.g. `--accent`, `--surface`, `--green`, `--warn`, `--red` + `-soft` tints) — never hard-code colors.
- Font: Inter. Inputs are 16px so iOS doesn't zoom on focus.
- Buttons: one primary style (`.btn-analyze`, `.btn-publish`, `.btn-*-batch`), one secondary (`.btn-sort-dump`, `.btn-add-batch`, `.btn-preview`), and `.btn-small` pills for everything in the header/toolbars.
- Publish is in a sticky bottom `.action-bar`; modals are bottom sheets on phones.
- No CSS framework — all styles in the `<style>` block in index.html (login.html repeats the tokens).

## How to Run
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py          # http://0.0.0.0:8080 — from phone: http://<mac-ip>:8080
```
Claude Code must be installed and logged in (`claude` once in Terminal). Restart: `lsof -ti:8080 | xargs kill; ./venv/bin/python app.py`.

## Access From Anywhere
Use Tailscale (no port forwarding). The app is not meant for the public internet — set `APP_PIN` anyway.

## Known TODOs
- [ ] Mercari selectors verified only against a mock — tune `MERCARI_UI` after the first real test-mode run
- [ ] Poshmark / Etsy / Depop adapters
- [ ] eBay condition options per category are hard-coded (6 values); could be fetched from the Metadata API
- [ ] No "save as draft" (unpublished offer) option yet
- [ ] Price CSV is a snapshot from the PriceCharting scraper (`~/PriceChartingScraper/pricecharting-scraper`, Sep 27 2026) — copy a fresh `pricecharting_master_price.csv` over it to refresh. Its Console names must match `PLATFORM_TO_CSV`.
- [ ] CSV has no Switch/PS5/Xbox Series — those fall back to Claude's estimate

## Context About the Developer
- Storm lists a retro game collection on eBay — mostly N64, SNES, PS1/PS2 era, some DS/3DS/Wii
- Mac, comfortable with Terminal, uses Claude Code as main dev tool
- Prefers automated workflows over manual copy-paste
- Keep code clean, well-commented, and in as few files as possible
