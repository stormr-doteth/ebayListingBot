# RetroList — eBay Game Lister
> Claude Code context file. Read this before making any changes.

## What This Is
A Flask web app that lets Storm photograph retro video games, identifies and grades them with Claude, prices them from a local PriceCharting CSV plus (optionally) live sold-price research, writes an honest eBay listing, and publishes it via the eBay Inventory API. Built to be used from a phone browser while the server runs on a Mac.

## Project Structure
```
ebayListingBot/
├── app.py                           # Flask backend — all routes, Claude pipeline, eBay publishing
├── templates/
│   ├── index.html                   # Single-page frontend, retro dark theme
│   └── login.html                   # PIN login (only used when APP_PIN is set)
├── pricecharting_master_price.csv   # Local price guide (~23k games, 14 consoles)
├── setup_policies.py                # One-time: create eBay business policies
├── setup_shipping_policy.py         # One-time: create calculated-shipping policy
├── requirements.txt
├── .env                             # Secrets/config (gitignored)
├── ebay_tokens.json                 # Saved eBay OAuth tokens (gitignored, created by /ebay/callback)
├── .flask_secret                    # Session key (gitignored, auto-created)
└── uploads/                         # Photos, auto-deleted after UPLOAD_RETENTION_DAYS (gitignored)
```

## Stack
- **Backend**: Python 3.13, Flask 3.x (runs in `venv/`), Pillow + pillow-heif for photos
- **AI**: the local **Claude Code CLI** in headless mode (`claude -p`). It runs on Storm's Claude plan login — no API key, no per-call billing. `run_claude()` removes `ANTHROPIC_API_KEY` from the subprocess env so it can never silently bill an API key.
- **Pricing**: `pricecharting_master_price.csv` (fuzzy-matched, several candidate rows so Claude can pick the right variant) + optional PriceCharting API + optional Claude WebSearch for recent sold comps
- **Listing**: eBay Inventory REST API + Trading API `UploadSiteHostedPictures` for images
- **Frontend**: Vanilla HTML/CSS/JS, no framework, Space Mono + Syne fonts

## Environment Variables (in `.env`)
```bash
# eBay (required to publish)
EBAY_TOKEN=v^1.1-...                   # Static token fallback (or use OAuth below)
EBAY_APP_ID= / EBAY_CERT_ID= / EBAY_REDIRECT_URI=   # OAuth "Connect eBay" button + auto-refresh
EBAY_SANDBOX=false
EBAY_FULFILLMENT_POLICY=255223329024
EBAY_PAYMENT_POLICY=255223242024
EBAY_RETURN_POLICY=255223411024
EBAY_LOCATION_KEY=RETRO_HQ             # Inventory location key (default RETRO_HQ)

# App (all optional)
APP_PIN=                               # Strongly recommended — without it anyone on the network can publish
HOST=0.0.0.0  PORT=8080
PRICECHARTING_API_KEY=
UPLOAD_RETENTION_DAYS=14

# Claude (all optional)
CLAUDE_MODEL=sonnet                    # or opus for harder IDs (slower, uses more plan quota)
WEB_PRICING=true                       # default for the "Research sold prices online" checkbox
ANALYZE_WORKERS=3                      # games analyzed in parallel
CLAUDE_TIMEOUT=240
CLAUDE_BIN=                            # path to `claude` if not on PATH
```
`GEMINI_API_KEY` / `CEREBRAS_API_KEY` are no longer used.

## Claude Pipeline (app.py)
Per game, run in a thread pool and streamed to the browser as SSE (`/analyze-batch`):
1. **`save_photos()`** — every upload is re-encoded to JPEG (HEIC converted, rotation fixed, EXIF/GPS stripped), max 2400px for eBay, plus a 1568px copy in `ai/` for Claude.
2. **`identify_game()`** — `claude -p` with the photos attached via `@photo_N.jpg` mentions. The CLI only attaches ~3 images, so with more photos the rest go on a numbered contact sheet (`make_contact_sheet`) and Claude may `Read` a full-size photo to zoom in. Returns `GAME_INFO_SCHEMA` (title, platform, variant, region, MPN, completeness, condition, authenticity, confidence, flags).
3. **`price_guide_matches()`** — top CSV rows (+ PriceCharting API if keyed).
4. **`write_listing()`** — second `claude -p` call (with `WebSearch` if web pricing is on) returns `LISTING_SCHEMA`: title, HTML description, condition, market prices, suggested price + range, comps, pricing flags.
5. **`_clean_game_info()` / `_clean_listing()`** — guard rails: 80-char title, strip "Authentic" unless authenticity is `likely_authentic`, valid condition enum, price ≥ $0.99, plain item-specific values.

Claude CLI safety: `--tools` limits tools to Read (+WebSearch); Read is *not* pre-approved so it only works inside the game's photo folder; `--setting-sources ""` ignores personal settings/hooks; `--no-session-persistence` keeps these runs out of session history. Photo text and web pages are treated as data (system prompt says so).

Typical timing: ~20–45s per game without web research, ~45–110s with it; games run 3 at a time.

## Routes
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Main UI |
| `/login` | GET/POST | PIN login (rate-limited: 5 tries / 5 min per IP) |
| `/analyze-batch` | POST | FormData `game_count`, `game_{n}_photos`, `game_{n}_notes`, `tested`, `web_pricing` → SSE `progress` / `result` / `error` / `done`. Used for single games too (count = 1). |
| `/publish` | POST | JSON `{listing, game_info, photos}` → `{success, listing_id, url}` or `{success: false, error}` |
| `/publish-batch` | POST | JSON `{games: [...]}` → `{results: [...]}` |
| `/ebay/auth`, `/ebay/callback` | GET | OAuth consent flow (with `state` check) |
| `/ebay/status`, `/ebay/disconnect` | GET/POST | Connection status / forget tokens |

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
- Options: "Tested & working" (controls whether the listing may say Tested) and "Research sold prices online" — remembered in localStorage.
- Single game and batch both stream progress from `/analyze-batch`.
- "AI check" panel per game: ID confidence, authenticity, variant, price range, flags, sold comps (with links), price-guide matches.
- Everything editable before publishing (title, description, condition, weight, price, item specifics). Published batch cards link to the live listing and can't be published twice.
- All model text is HTML-escaped (`esc()`) before rendering.

## Design System
- Background `#0a0a0f`, Surface `#13131a`, Accent `#ff6b2b`, Yellow `#ffcc00`, Green `#39ff14`
- Fonts: Space Mono (body), Syne (display). Retro terminal look: scanlines + grid background.
- No CSS framework — all styles in the `<style>` block in index.html

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
- [ ] eBay condition options per category are hard-coded (6 values); could be fetched from the Metadata API
- [ ] No "save as draft" (unpublished offer) option yet
- [ ] Price CSV is a static snapshot — refresh it occasionally

## Context About the Developer
- Storm lists a retro game collection on eBay — mostly N64, SNES, PS1/PS2 era, some DS/3DS/Wii
- Mac, comfortable with Terminal, uses Claude Code as main dev tool
- Prefers automated workflows over manual copy-paste
- Keep code clean, well-commented, and in as few files as possible
