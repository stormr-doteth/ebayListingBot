# RetroList — eBay Game Lister
> Claude Code context file. Read this before making any changes.

## What This Is
A Flask web app that lets Storm photograph retro video game cartridges, automatically identifies them using Claude Vision, fetches current market prices, generates optimized eBay listings, and publishes them directly via the eBay Inventory API. Built to be accessed from a phone browser while the server runs on a Mac.

## Project Structure
```
ebay-lister/
├── app.py                  # Flask backend — all routes and agent logic
├── templates/
│   └── index.html          # Single-page frontend, retro dark theme
├── requirements.txt
├── uploads/                # Temp storage for uploaded photos (gitignored)
└── CLAUDE.md               # This file
```

## Stack
- **Backend**: Python 3, Flask 3.x
- **AI**: Google Gemini API (`gemini-2.5-flash`) for image analysis + listing generation (free tier)
- **Pricing**: PriceCharting API (optional key, falls back to Claude estimate)
- **Listing**: eBay Inventory REST API (OAuth bearer token)
- **Frontend**: Vanilla HTML/CSS/JS, no framework, Space Mono + Syne fonts

## Environment Variables
```bash
GEMINI_API_KEY=AIza...                 # Required (free from aistudio.google.com)
EBAY_TOKEN=v^1.1-...                   # Required for publishing to eBay
EBAY_FULFILLMENT_POLICY=XXXXXXXX       # eBay business policy ID
EBAY_PAYMENT_POLICY=XXXXXXXX           # eBay business policy ID
EBAY_RETURN_POLICY=XXXXXXXX            # eBay business policy ID
PRICECHARTING_API_KEY=...              # Optional
```

## API Routes
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Serves the main UI |
| `/analyze` | POST | Accepts multipart photo + notes, runs full agent pipeline, returns JSON |
| `/publish` | POST | Accepts JSON listing data, posts to eBay API, returns result |

## Agent Pipeline (in app.py)
1. **`analyze_game_photo(image_path)`** — sends image to Gemini Vision, returns structured game info (title, platform, year, region, condition, has_box, has_manual, publisher, genre, rating, mpn)
2. **`get_market_price(game_title, platform)`** — queries PriceCharting API for loose/CIB/new prices
3. **`generate_listing(game_info, price_info, extra_notes)`** — prompts Claude to write optimized eBay title, HTML description, condition grade, and suggested price
4. **`post_to_ebay(listing, game_info)`** — creates eBay inventory item (with item specifics/aspects) → creates offer → publishes listing

## eBay API Flow
Uses the modern eBay Inventory REST API (not the legacy Trading API):
1. `PUT /sell/inventory/v1/inventory_item/{sku}` — create product record with `product.aspects` (item specifics)
2. `POST /sell/inventory/v1/offer` — create offer with price + policies
3. `POST /sell/inventory/v1/offer/{offerId}/publish` — go live

eBay category ID `139973` = Video Games (used for all retro game listings).

## Frontend Behavior
- Mobile-first, works as phone camera → instant listing tool
- Drag-and-drop or file picker (uses `capture="environment"` for phone camera)
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
- [ ] eBay OAuth token setup not yet done (needs developer.ebay.com account + user token flow)
- [ ] PriceCharting API key not configured (works without it, prices less accurate)
- [ ] No image upload to eBay listing yet (eBay requires hosted image URLs)
- [ ] No multi-photo support (single photo only)
- [ ] Uploads folder not cleaned up automatically
- [ ] No authentication on the web UI (fine for local/Tailscale use, not for public internet)

## How to Run
```bash
pip install -r requirements.txt
export GEMINI_API_KEY="AIza..."
python app.py
# Open http://localhost:8080
# From phone on same WiFi: http://192.168.1.XXX:8080
```

## Access From Anywhere
Storm uses (or plans to use) Tailscale for remote access so the app is reachable from phone even off home WiFi. No port forwarding needed.

## Context About the Developer
- Storm is building this to list a retro game collection on eBay faster
- Primarily sells N64, SNES, PS1/PS2 era cartridges and discs
- Running on Mac, comfortable with Terminal, uses Claude Code as main dev tool
- Preference for automated workflows over manual copy-paste
- Keep code clean, well-commented, and in as few files as possible
