import os
import json
import time
import requests
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from google import genai

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Gemini client (free tier)
gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"

EBAY_TOKEN = os.environ.get("EBAY_TOKEN", "YOUR_EBAY_TOKEN_HERE")
EBAY_FULFILLMENT_POLICY = os.environ.get("EBAY_FULFILLMENT_POLICY", "")
EBAY_PAYMENT_POLICY = os.environ.get("EBAY_PAYMENT_POLICY", "")
EBAY_RETURN_POLICY = os.environ.get("EBAY_RETURN_POLICY", "")

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────

def parse_json_response(text: str) -> dict:
    """Strip markdown fences and parse JSON from model output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ─────────────────────────────────────────
#  Core agent functions
# ─────────────────────────────────────────

def analyze_game_photo(image_path: str) -> dict:
    """Send photo to Gemini Vision, get structured game info back."""
    image_file = gemini.files.upload(file=image_path)

    response = gemini.models.generate_content(
        model=MODEL,
        contents=[
            image_file,
            """Identify this retro video game cartridge or disc. Return ONLY valid JSON with no extra text:
{
  "game_title": "exact game title",
  "platform": "Nintendo 64",
  "year": "1997",
  "region": "USA",
  "condition_notes": "brief description of label/cartridge physical condition",
  "has_box": false,
  "has_manual": false,
  "authenticity": "authentic",
  "publisher": "Nintendo",
  "genre": "Platformer",
  "rating": "E-Everyone",
  "mpn": "NUS-NSME-USA"
}
If you can read the MPN/part number off the cartridge or disc, use that. Otherwise give your best guess or "N/A"."""
        ]
    )
    return parse_json_response(response.text)


def get_market_price(game_title: str, platform: str) -> dict:
    """Query PriceCharting for market data."""
    try:
        api_key = os.environ.get("PRICECHARTING_API_KEY", "")
        search = f"{game_title} {platform}"
        url = f"https://www.pricecharting.com/api/products?q={requests.utils.quote(search)}&status=price"
        if api_key:
            url += f"&id={api_key}"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        products = data.get("products", [])
        if products:
            p = products[0]
            return {
                "loose_price": p.get("loose-price", 0) / 100,
                "cib_price": p.get("cib-price", 0) / 100,
                "new_price": p.get("new-price", 0) / 100,
                "source": "pricecharting"
            }
    except Exception:
        pass
    # Fallback: return zeroes, Gemini will estimate
    return {"loose_price": 0, "cib_price": 0, "new_price": 0, "source": "estimated"}


def generate_listing(game_info: dict, price_info: dict, extra_notes: str = "") -> dict:
    """Use Gemini to write an optimized eBay listing."""
    loose = price_info["loose_price"]
    cib = price_info["cib_price"]
    has_box = game_info.get("has_box", False)
    has_manual = game_info.get("has_manual", False)

    market_price = cib if (has_box and has_manual) else loose
    price_hint = f"Market price: ${market_price:.2f}" if market_price > 0 else "Estimate a fair market price for this game."

    prompt = f"""Create an optimized eBay listing for this retro video game.

Game: {game_info['game_title']}
Platform: {game_info['platform']}
Year: {game_info.get('year', 'Unknown')}
Region: {game_info.get('region', 'USA')}
Condition: {game_info['condition_notes']}
Has box: {has_box}
Has manual: {has_manual}
Authenticity: {game_info.get('authenticity', 'authentic')}
{price_hint}
Extra seller notes: {extra_notes or 'None'}

Return ONLY valid JSON with no extra text:
{{
  "title": "eBay title max 80 chars, include game name, platform, year, Authentic, Tested",
  "description": "2-3 sentence HTML description. Be direct: state condition, that it's tested/working, and shipping speed. No fluff.",
  "condition": "USED_GOOD",
  "suggested_price": 44.99,
  "price_reasoning": "one sentence why this price"
}}

Price 5-10% below market to sell quickly. Condition options: USED_LIKE_NEW, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE."""

    response = gemini.models.generate_content(model=MODEL, contents=prompt)
    return parse_json_response(response.text)


def post_to_ebay(listing: dict, game_info: dict) -> dict:
    """Post the listing to eBay via Inventory API."""
    if not EBAY_TOKEN or EBAY_TOKEN == "YOUR_EBAY_TOKEN_HERE":
        return {"success": False, "error": "No eBay token configured", "listing_id": None}

    headers = {
        "Authorization": f"Bearer {EBAY_TOKEN}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
    }

    safe_title = listing['title'][:20].replace(' ', '-').upper()
    sku = f"RETRO-{safe_title}-{int(time.time())}"

    # Build item specifics (aspects)
    region = game_info.get("region", "USA")
    region_code = "NTSC-U/C" if region in ("USA", "US", "NA") else "PAL" if region in ("PAL", "EUR", "Europe") else "NTSC-J" if region in ("Japan", "JP") else region

    includes = ["Game"]
    if game_info.get("has_manual"):
        includes.append("Manual")
    if game_info.get("has_box"):
        includes.append("Box")
    features = ", ".join(includes)

    aspects = {
        "Game Name": [game_info.get("game_title", "Unknown")],
        "Platform": [game_info.get("platform", "Unknown")],
        "Publisher": [game_info.get("publisher", "Unknown")],
        "Genre": [game_info.get("genre", "Unknown")],
        "Rating": [game_info.get("rating", "Unknown")],
        "Release Year": [str(game_info.get("year", "Unknown"))],
        "Region Code": [region_code],
        "MPN": [game_info.get("mpn", "N/A")],
        "Features": [features],
    }

    # 1. Create inventory item
    inventory_url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
    inventory_payload = {
        "product": {
            "title": listing["title"],
            "description": listing["description"],
            "aspects": aspects,
        },
        "condition": listing["condition"],
        "availability": {
            "shipToLocationAvailability": {"quantity": 1}
        }
    }
    inv_resp = requests.put(inventory_url, json=inventory_payload, headers=headers)
    if inv_resp.status_code not in [200, 201, 204]:
        return {"success": False, "error": f"Inventory error: {inv_resp.text}", "listing_id": None}

    # 2. Create offer
    offer_url = "https://api.ebay.com/sell/inventory/v1/offer"
    offer_payload = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": "139973",  # Video Games
        "pricingSummary": {
            "price": {"value": str(listing["suggested_price"]), "currency": "USD"}
        },
        "listingPolicies": {
            "fulfillmentPolicyId": EBAY_FULFILLMENT_POLICY,
            "paymentPolicyId": EBAY_PAYMENT_POLICY,
            "returnPolicyId": EBAY_RETURN_POLICY
        }
    }
    offer_resp = requests.post(offer_url, json=offer_payload, headers=headers)
    if offer_resp.status_code not in [200, 201]:
        return {"success": False, "error": f"Offer error: {offer_resp.text}", "listing_id": None}

    offer_id = offer_resp.json().get("offerId")

    # 3. Publish
    publish_url = f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish"
    pub_resp = requests.post(publish_url, headers=headers)
    if pub_resp.status_code in [200, 201]:
        listing_id = pub_resp.json().get("listingId")
        return {"success": True, "listing_id": listing_id, "sku": sku}
    else:
        return {"success": False, "error": f"Publish error: {pub_resp.text}", "listing_id": None}


# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    if "photo" not in request.files:
        return jsonify({"error": "No photo uploaded"}), 400

    photo = request.files["photo"]
    extra_notes = request.form.get("notes", "")

    # Save uploaded file
    ext = Path(photo.filename).suffix or ".jpg"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], f"upload{ext}")
    photo.save(save_path)

    try:
        # Step 1: Analyze
        game_info = analyze_game_photo(save_path)

        # Step 2: Market price
        price_info = get_market_price(game_info["game_title"], game_info["platform"])

        # Step 3: Generate listing
        listing = generate_listing(game_info, price_info, extra_notes)

        return jsonify({
            "game_info": game_info,
            "price_info": price_info,
            "listing": listing
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/publish", methods=["POST"])
def publish():
    data = request.json
    listing = data.get("listing")
    game_info = data.get("game_info")

    if not listing or not game_info:
        return jsonify({"error": "Missing data"}), 400

    result = post_to_ebay(listing, game_info)
    return jsonify(result)


if __name__ == "__main__":
    os.makedirs("uploads", exist_ok=True)
    app.run(host="0.0.0.0", port=8080, debug=True)
