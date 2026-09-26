# RetroList

Photograph a retro game → get an honest, well-priced eBay listing → publish it. Runs on your Mac and is used from your phone.

- **Claude identifies and grades** each game from all your photos: exact title, variant (Player's Choice, Greatest Hits…), region, part number, what's included, visible wear, and whether it looks authentic.
- **Prices** are the PriceCharting loose or CIB value for exactly what you have (correct variant, e.g. Player's Choice).
- **Flags** tell you what to double-check before you publish.
- **Photo dump**: select every photo of a pile of games at once — it sorts them by when they were taken and splits them into one listing per game (you can fix any split before analyzing).
- **Batch mode** analyzes several games in parallel.
- **Cross-listing** (▦ Inventory): every published game is tracked; list it on Mercari with one tap. When a game sells anywhere, it's taken down everywhere else automatically.

### Mercari setup (optional)
```bash
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium
./venv/bin/python mercari.py login     # log in once in the window that opens
```
Starts in **dry-run** mode (fills the form, saves a screenshot to `mercari_debug/`, doesn't list). Check a couple, then add `MERCARI_SUBMIT=true` to `.env`. If Mercari changes its site: `./venv/bin/python mercari.py probe`.

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
Then open `http://<your-mac-ip>:8080` on your phone (same Wi-Fi, or use Tailscale from anywhere).
