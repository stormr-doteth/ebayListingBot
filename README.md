# RetroList

Photograph a retro game → get an honest, well-priced eBay listing → publish it. Runs on your Mac and is used from your phone.

- **Claude identifies and grades** each game from all your photos: exact title, variant (Player's Choice, Greatest Hits…), region, part number, what's included, visible wear, and whether it looks authentic.
- **Prices** are the PriceCharting loose or CIB value for exactly what you have (correct variant, e.g. Player's Choice).
- **Flags** tell you what to double-check before you publish.
- **Photo dump**: select every photo of a pile of games at once — it sorts them by when they were taken and splits them into one listing per game (you can fix any split before analyzing).
- **Batch mode** analyzes several games in parallel.
- **Cross-list to Mercari**: tick “Mercari” and each game is listed there too. When it sells on either site, the other listing is ended automatically.

AI runs through the [Claude Code](https://claude.com/claude-code) CLI on your own Mac, so it uses your Claude plan instead of a paid API key.

## Setup
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
claude            # once, to log in to Claude Code (if you haven't already)
```

Create a `.env` file (see `CLAUDE.md` for every option):
```bash
EBAY_TOKEN=v^1.1-...            # or EBAY_APP_ID / EBAY_CERT_ID / EBAY_REDIRECT_URI for OAuth
EBAY_FULFILLMENT_POLICY=...
EBAY_PAYMENT_POLICY=...
EBAY_RETURN_POLICY=...
APP_PIN=1234                    # recommended
```

## Run
```bash
./venv/bin/python app.py
```
### Mercari (optional)
```bash
./venv/bin/playwright install chromium   # only needed if Google Chrome isn't installed
```
Log in once: `./venv/bin/python crosslist.py login` (or tick **Mercari** in the app and tap **Log in to Mercari**).
Games already on eBay: open **📦 Inventory → Import eBay listings**, then tap **+ Mercari** on any of them.
It starts in **test mode** (`MERCARI_DRY_RUN=true`): it fills Mercari's form and saves a screenshot, but never posts.
Check a few screenshots (tap the yellow “test fill” badge), then set `MERCARI_DRY_RUN=false` in `.env` and restart.
Mercari has no public API, so this drives a browser — keep it to normal human volumes.

Then open `http://<your-mac-ip>:8080` on your phone (same Wi-Fi, or use Tailscale from anywhere).
