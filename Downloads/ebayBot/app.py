import os
import json
import time
import requests
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from google import genai
from cerebras.cloud.sdk import Cerebras

load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64MB max (multi-image)

# Gemini client (vision only)
gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
GEMINI_MODEL = "gemini-3-flash-preview"

# Cerebras client (listing text generation)
cerebras = Cerebras(api_key=os.environ.get("CEREBRAS_API_KEY"))
CEREBRAS_MODEL = "llama3.1-8b"

EBAY_TOKEN = os.environ.get("EBAY_TOKEN", "YOUR_EBAY_TOKEN_HERE")
EBAY_SANDBOX = os.environ.get("EBAY_SANDBOX", "false").lower() == "true"
EBAY_BASE_URL = "https://api.sandbox.ebay.com" if EBAY_SANDBOX else "https://api.ebay.com"
EBAY_FULFILLMENT_POLICY = os.environ.get("EBAY_FULFILLMENT_POLICY", "")
EBAY_PAYMENT_POLICY = os.environ.get("EBAY_PAYMENT_POLICY", "")
EBAY_RETURN_POLICY = os.environ.get("EBAY_RETURN_POLICY", "")

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────

def upload_to_ebay_eps(image_path: str) -> str:
    """Upload image to eBay via Trading API UploadSiteHostedPictures, return ebayimg.com URL."""
    token = os.environ.get("EBAY_TOKEN", "")
    if not token or token == "YOUR_EBAY_TOKEN_HERE":
        return ""

    # Trading API endpoint (production)
    api_url = "https://api.ebay.com/ws/api.dll"
    if EBAY_SANDBOX:
        api_url = "https://api.sandbox.ebay.com/ws/api.dll"

    headers = {
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
        "X-EBAY-API-IAF-TOKEN": token,
    }

    # XML request body
    xml_body = """<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <PictureName>{name}</PictureName>
</UploadSiteHostedPicturesRequest>""".format(name=os.path.basename(image_path))

    try:
        with open(image_path, "rb") as f:
            image_data = f.read()

        # Multipart: XML part + binary image part
        files = {
            "XML Payload": ("payload.xml", xml_body.encode("utf-8"), "text/xml"),
            "image": (os.path.basename(image_path), image_data, "image/jpeg"),
        }
        resp = requests.post(api_url, headers=headers, files=files, timeout=60)

        # Parse URL from XML response
        text = resp.text
        if "<FullURL>" in text:
            url = text.split("<FullURL>")[1].split("</FullURL>")[0]
            print(f"[eps] Uploaded: {url}")
            return url
        else:
            print(f"[eps] No FullURL in response: {text[:300]}")
            return ""
    except Exception as e:
        print(f"[eps] Upload error: {e}")
        return ""


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
        model=GEMINI_MODEL,
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
    """Use Cerebras (Llama 3.3 70B) to write an optimized eBay listing."""
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

    response = cerebras.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json_response(response.choices[0].message.content)


def post_to_ebay(listing: dict, game_info: dict, image_urls: list = None) -> dict:
    """Post the listing to eBay via Inventory API."""
    token = os.environ.get("EBAY_TOKEN", "")
    print(f"[publish] Token loaded = {bool(token and token != 'YOUR_EBAY_TOKEN_HERE')}")
    if not token or token == "YOUR_EBAY_TOKEN_HERE":
        return {"success": False, "error": "No eBay token configured", "listing_id": None}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
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
    inventory_url = f"{EBAY_BASE_URL}/sell/inventory/v1/inventory_item/{sku}"
    product = {
        "title": listing["title"],
        "description": listing["description"],
        "aspects": aspects,
    }
    if image_urls:
        product["imageUrls"] = image_urls

    inventory_payload = {
        "product": product,
        "condition": listing["condition"],
        "packageWeightAndSize": {
            "weight": {
                "value": listing.get("weight_oz", 8),
                "unit": "OUNCE"
            }
        },
        "availability": {
            "shipToLocationAvailability": {"quantity": 1}
        }
    }
    print(f"[publish] SKU={sku}, images={len(image_urls or [])}, condition={listing['condition']}")
    # Retry once on eBay internal errors (25001)
    for attempt in range(2):
        inv_resp = requests.put(inventory_url, json=inventory_payload, headers=headers)
        if inv_resp.status_code in [200, 201, 204]:
            break
        # Check if it's a retryable internal error
        if attempt == 0 and inv_resp.status_code >= 500:
            print(f"[publish] Inventory attempt 1 failed ({inv_resp.status_code}), retrying...")
            time.sleep(2)
            continue
        # Also retry on error 25001 (comes back as 400/500 with internal error)
        if attempt == 0 and "25001" in inv_resp.text:
            print(f"[publish] Got error 25001, retrying...")
            time.sleep(2)
            continue
        return {"success": False, "error": f"Inventory error: {inv_resp.text}", "listing_id": None}
    if inv_resp.status_code not in [200, 201, 204]:
        return {"success": False, "error": f"Inventory error: {inv_resp.text}", "listing_id": None}

    # 2. Create offer
    offer_url = f"{EBAY_BASE_URL}/sell/inventory/v1/offer"
    offer_payload = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": "139973",  # Video Games
        "merchantLocationKey": "RETRO_HQ",
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
    publish_url = f"{EBAY_BASE_URL}/sell/inventory/v1/offer/{offer_id}/publish"
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
    photos = request.files.getlist("photos")
    if not photos or not photos[0].filename:
        return jsonify({"error": "No photos uploaded"}), 400

    extra_notes = request.form.get("notes", "")

    # Save all uploaded files
    image_paths = []
    for i, photo in enumerate(photos):
        ext = Path(photo.filename).suffix or ".jpg"
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], f"upload_{i}{ext}")
        photo.save(save_path)
        image_paths.append(save_path)

    try:
        # Step 1: Analyze (first image only)
        game_info = analyze_game_photo(image_paths[0])

        # Step 2: Market price
        price_info = get_market_price(game_info["game_title"], game_info["platform"])

        # Step 3: Generate listing
        listing = generate_listing(game_info, price_info, extra_notes)

        return jsonify({
            "game_info": game_info,
            "price_info": price_info,
            "listing": listing,
            "image_paths": image_paths
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/publish", methods=["POST"])
def publish():
    data = request.json
    listing = data.get("listing")
    game_info = data.get("game_info")
    image_paths = data.get("image_paths", [])

    if not listing or not game_info:
        return jsonify({"error": "Missing data"}), 400

    # Upload images to eBay EPS
    image_urls = []
    for path in image_paths:
        if os.path.exists(path):
            url = upload_to_ebay_eps(path)
            if url:
                image_urls.append(url)

    result = post_to_ebay(listing, game_info, image_urls=image_urls)
    return jsonify(result)


if __name__ == "__main__":
    os.makedirs("uploads", exist_ok=True)
    # Debug: confirm env vars loaded
    print(f"[startup] EBAY_SANDBOX = {EBAY_SANDBOX}")
    print(f"[startup] EBAY_BASE_URL = {EBAY_BASE_URL}")
    print(f"[startup] EBAY_TOKEN set = {EBAY_TOKEN not in (None, '', 'YOUR_EBAY_TOKEN_HERE')}")
    print(f"[startup] EBAY_TOKEN first 20 chars = {EBAY_TOKEN[:20] if EBAY_TOKEN else 'EMPTY'}...")
    app.run(host="0.0.0.0", port=8080, debug=True)
