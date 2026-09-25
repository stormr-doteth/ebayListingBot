# RetroList

Photograph a retro game → get an honest, well-priced eBay listing → publish it. Runs on your Mac and is used from your phone.

- **Claude identifies and grades** each game from all your photos: exact title, variant (Player's Choice, Greatest Hits…), region, part number, what's included, visible wear, and whether it looks authentic.
- **Prices** come from a local PriceCharting guide plus (optionally) recent sold comps researched online.
- **Flags** tell you what to double-check before you publish.
- **Batch mode** analyzes several games in parallel.

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
