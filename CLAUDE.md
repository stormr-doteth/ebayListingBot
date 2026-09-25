# RetroList — eBay Game Lister
> Claude Code context file. Read this before making any changes.

## What This Is
A Flask web app that lets Storm photograph retro video games, identifies and grades them with Claude, prices them at the PriceCharting loose or CIB value, writes an honest eBay listing, and publishes it via the eBay Inventory API. Built to be used from a phone browser while the server runs on a Mac.

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
├── ebay_tokens.json                 # Saved eBay OAuth tokens (gitignored)
├── ebay_config.json                 # eBay App ID / Cert ID / RuName from the setup screen (gitignored)
├── .flask_secret                    # Session key (gitignored, auto-created)
└── uploads/                         # Photos, auto-deleted after UPLOAD_RETENTION_DAYS (gitignored)
```

## Stack
- **Backend**: Python 3.13, Flask 3.x (runs in `venv/`), Pillow + pillow-heif for photos
- **AI**: the local **Claude Code CLI** in headless mode (`claude -p`). It runs on Storm's Claude plan login — no API key, no per-call billing. `run_claude()` removes `ANTHROPIC_API_KEY` from the subprocess env so it can never silently bill an API key.
- **Pricing**: PriceCharting loose/CIB price — local `pricecharting_master_price.csv` snapshot, or the live PriceCharting API when `PRICECHARTING_API_KEY` is set. No web research.
- **Listing**: eBay Inventory REST API + Trading API `UploadSiteHostedPictures` for images
- **Frontend**: Vanilla HTML/CSS/JS, no framework, Space Mono + Syne fonts

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
```
`GEMINI_API_KEY` / `CEREBRAS_API_KEY` are no longer used.

## Claude Pipeline (app.py)
Per game, run in a thread pool and streamed to the browser as SSE (`/analyze-batch`):
1. **`save_photos()`** — every upload is re-encoded to JPEG (HEIC converted, rotation fixed, EXIF/GPS stripped), max 2400px for eBay, plus a 1568px copy in `ai/` for Claude.
2. **`analyze_photos()`** — ONE `claude -p` call with the photos attached via `@photo_N.jpg` mentions (the CLI only attaches ~3, so extras go on a numbered contact sheet and Claude may `Read` a full-size photo). Returns `GAME_INFO_SCHEMA`: identification (title, platform, variant, region, MPN, completeness), condition, authenticity, confidence, flags, **plus the eBay title + HTML description** and a rough `estimated_loose/cib` fallback.
3. **`price_listing()`** — pricing is deterministic, not Claude's call: PriceCharting API rows if `PRICECHARTING_API_KEY` is set, else the local CSV. `pick_price_row()` chooses the row for the exact variant (uses the variant field, listing title and MPN suffix like `GH`). Game + box + manual → **CIB** price; anything less → **loose**. Not in the guide → Claude's estimate, flagged.
4. **`_clean_game_info()` / `_clean_title()`** — guard rails: plain item-specific values, 80-char title, strip "Authentic" unless authenticity is `likely_authentic`.

The UI shows which PriceCharting row was used (linked) and the other candidate rows with one-tap Loose/CIB price buttons.

Claude CLI safety: `--tools Read` only, and Read is *not* pre-approved so it only works inside the game's photo folder; `--setting-sources ""` ignores personal settings/hooks; `--no-session-persistence` keeps these runs out of session history. Photo text is treated as data (system prompt says so).

Typical cost: ~20–40k tokens and ~15–25s per game with 2–4 photos (more photos → more). Token usage is logged per game and shown in the AI check panel. Games run 3 at a time.

## Routes
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Main UI |
| `/login` | GET/POST | PIN login (rate-limited: 5 tries / 5 min per IP) |
| `/analyze-batch` | POST | FormData `game_count`, `game_{n}_photos`, `game_{n}_notes`, `tested` → SSE `progress` / `result` / `error` / `done`. Used for single games too (count = 1). |
| `/publish` | POST | JSON `{listing, game_info, photos}` → `{success, listing_id, url}` or `{success: false, error}` |
| `/publish-batch` | POST | JSON `{games: [...]}` → `{results: [...]}` |
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
- [ ] Price CSV is a static snapshot (Mar 2026) — refresh it, or add a PriceCharting API key for live prices
- [ ] CSV has no 3DS/Switch/PS4+ — those fall back to Claude's estimate

## Context About the Developer
- Storm lists a retro game collection on eBay — mostly N64, SNES, PS1/PS2 era, some DS/3DS/Wii
- Mac, comfortable with Terminal, uses Claude Code as main dev tool
- Prefers automated workflows over manual copy-paste
- Keep code clean, well-commented, and in as few files as possible
