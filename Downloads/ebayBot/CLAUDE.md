# RetroList — eBay Game Lister
> Claude Code context file. Read this before making any changes.

## What This Is
A Flask web app that lets Storm photograph retro video game cartridges, automatically identifies them using Gemini Vision, fetches current market prices, generates optimized eBay listings, and publishes them directly via the eBay Inventory API. Built to be accessed from a phone browser while the server runs on a Mac.

## Project Structure
```
ebayBot/
├── app.py                  # Flask backend — all routes and agent logic
├── templates/
│   └── index.html          # Single-page frontend, retro dark theme
├── requirements.txt
├── .env                    # Environment variables (gitignored)
├── venv/                   # Python virtual environment
├── uploads/                # Temp storage for uploaded photos (gitignored)
└── CLAUDE.md               # This file
```

## Stack
- **Backend**: Python 3, Flask 3.x (runs in `venv/`)
- **AI (Vision)**: Google Gemini API (`gemini-3-flash-preview`) for image analysis (free tier)
- **AI (Text)**: Cerebras (`llama3.1-8b`) for listing text generation (free tier, 1M tokens/day, 30 RPM)
- **Pricing**: PriceCharting API (optional key, falls back to Cerebras estimate)
- **Listing**: eBay Inventory REST API (OAuth bearer token) + Trading API for image uploads
- **Frontend**: Vanilla HTML/CSS/JS, no framework, Space Mono + Syne fonts

## Environment Variables (in `.env`)
```bash
GEMINI_API_KEY=AIza...                 # Required (free from aistudio.google.com)
EBAY_TOKEN=v^1.1-...                   # Required for publishing to eBay
EBAY_SANDBOX=false                     # Set to "true" to use sandbox API
EBAY_FULFILLMENT_POLICY=255223329024   # eBay business policy ID (USPS Ground Advantage - Calculated)
EBAY_PAYMENT_POLICY=255223242024       # eBay business policy ID
EBAY_RETURN_POLICY=255223411024        # eBay business policy ID
PRICECHARTING_API_KEY=                 # Optional
CEREBRAS_API_KEY=                      # Required (free from cerebras.ai)
```

### Finding eBay Policy IDs
Policy IDs are 12-digit numbers (e.g. `255223329024`). To list them via API:
```bash
source .env
curl -s -H "Authorization: Bearer $EBAY_TOKEN" \
  "https://api.ebay.com/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US" | python3 -m json.tool
# Also: /payment_policy and /return_policy
```

## API Routes
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serves the main UI |
| `/analyze` | POST | Accepts multipart photos + notes, runs full agent pipeline, returns JSON |
| `/publish` | POST | Accepts JSON listing data, uploads images to eBay, posts listing, returns result |

## Agent Pipeline (in app.py)
1. **`analyze_game_photo(image_path)`** — sends image to Gemini Vision, returns structured game info (title, platform, year, region, condition, has_box, has_manual, publisher, genre, rating, mpn)
2. **`get_market_price(game_title, platform)`** — queries PriceCharting API for loose/CIB/new prices
3. **`generate_listing(game_info, price_info, extra_notes)`** — prompts Cerebras (Llama 3.1 8B) to write optimized eBay title, HTML description, condition grade, and suggested price
4. **`upload_to_ebay_eps(image_path)`** — uploads image to eBay via Trading API `UploadSiteHostedPictures`, returns `ebayimg.com` URL
5. **`post_to_ebay(listing, game_info, image_urls)`** — creates eBay inventory item (with item specifics/aspects + image URLs) → creates offer → publishes listing

## eBay API Flow
Uses the modern eBay Inventory REST API + legacy Trading API for image uploads:
1. `POST /ws/api.dll` (Trading API `UploadSiteHostedPictures`) — upload images, get `ebayimg.com` URLs
2. `PUT /sell/inventory/v1/inventory_item/{sku}` — create product record with `product.aspects` + `product.imageUrls`
3. `POST /sell/inventory/v1/offer` — create offer with price + policies
4. `POST /sell/inventory/v1/offer/{offerId}/publish` — go live

eBay category ID `139973` = Video Games (used for all retro game listings).

**Known eBay quirks:**
- Error 25001 ("Core Inventory Service internal error") is transient — the app retries once automatically
- Error 25002 ("Add at least 1 photo") means image upload to EPS failed — check token permissions
- `packageType` in inventory payload causes error 25101 for video games — omitted intentionally, only weight is sent
- The OAuth token (`EBAY_TOKEN`) works for both Inventory REST API and Trading API (via `X-EBAY-API-IAF-TOKEN` header)

## Frontend Behavior
- Mobile-first, works as phone camera → instant listing tool
- Multi-image upload: drag-and-drop or file picker, shows thumbnail strip with remove buttons
- First photo is used for AI identification (marked with "ID Photo" badge), all photos sent to eBay listing gallery
- "+" button to add more photos after initial selection
- Three-step status display during analysis (identifying / pricing / generating)
- Item specifics displayed in game card (publisher, genre, rating, MPN) for verification
- Editable title (80 char limit with counter), price, and condition grade before publishing
- Description preview modal shows rendered HTML before posting
- If no EBAY_TOKEN, publish falls back to showing copy-pasteable listing data

## Design System
- Background: `#0a0a0f`, Surface: `#13131a`, Accent: `#ff6b2b` (orange), Green: `#39ff14`
- Fonts: Space Mono (body/mono), Syne (display/headings)
- Aesthetic: retro terminal / arcade dark theme with scanline overlay and grid background
- No CSS framework — all custom styles in `<style>` block in index.html

## Current Limitations / Known TODOs
- [ ] eBay OAuth token requires manual refresh (no auto-refresh flow yet)
- [ ] PriceCharting API key not configured (works without it, prices less accurate)
- [ ] Uploads folder not cleaned up automatically
- [ ] No authentication on the web UI (fine for local/Tailscale use, not for public internet)

## Cerebras Free Tier Notes
- Available models: `llama3.1-8b` (8K context) and `gpt-oss-120b` (65K context)
- `llama-3.3-70b` is NOT available on the free tier (404 error)
- Free tier limits: 30 RPM, 900/hour, 14,400/day, 1M tokens/day
- If upgrading to paid, switch `CEREBRAS_MODEL` in app.py to a larger model for better listings

## How to Run
```bash
# First time setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Start the server (MUST use the venv Python)
./venv/bin/python3 app.py
# Server runs on http://0.0.0.0:8080

# From phone on same WiFi: http://192.168.254.174:8080
```

**Important:** Always use `./venv/bin/python3 app.py` (not bare `python3`) — Flask and other deps are installed in the venv, not system Python.

To restart: kill the existing process on port 8080, then run again:
```bash
lsof -ti:8080 | xargs kill -9
./venv/bin/python3 app.py
```

## Access From Anywhere
Storm uses (or plans to use) Tailscale for remote access so the app is reachable from phone even off home WiFi. No port forwarding needed.

## Context About the Developer
- Storm is building this to list a retro game collection on eBay faster
- Primarily sells N64, SNES, PS1/PS2 era cartridges and discs
- Running on Mac, comfortable with Terminal, uses Claude Code as main dev tool
- Preference for automated workflows over manual copy-paste
- Keep code clean, well-commented, and in as few files as possible
