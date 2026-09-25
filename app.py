import os
import csv
import json
import time
import base64
import difflib
import functools
import re
import requests
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for

load_dotenv(Path(__file__).parent / ".env")

from google import genai
from cerebras.cloud.sdk import Cerebras

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 160 * 1024 * 1024  # 160MB max (batch: 5 games x 4 photos x ~5MB)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())

# PIN-based authentication
APP_PIN = os.environ.get("APP_PIN", "")

def require_pin(f):
    """Decorator: redirect to login if not authenticated."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if APP_PIN and not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# Gemini client (vision only)
gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
GEMINI_MODEL = "gemini-3-flash-preview"

# Cerebras client (listing text generation)
cerebras = Cerebras(api_key=os.environ.get("CEREBRAS_API_KEY"))
CEREBRAS_MODEL = "llama3.1-8b"

EBAY_SANDBOX = os.environ.get("EBAY_SANDBOX", "false").lower() == "true"
EBAY_BASE_URL = "https://api.sandbox.ebay.com" if EBAY_SANDBOX else "https://api.ebay.com"
EBAY_AUTH_URL = "https://auth.sandbox.ebay.com" if EBAY_SANDBOX else "https://auth.ebay.com"
EBAY_FULFILLMENT_POLICY = os.environ.get("EBAY_FULFILLMENT_POLICY", "")
EBAY_PAYMENT_POLICY = os.environ.get("EBAY_PAYMENT_POLICY", "")
EBAY_RETURN_POLICY = os.environ.get("EBAY_RETURN_POLICY", "")

# OAuth2 credentials (from eBay Developer dashboard)
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")        # Client ID
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")      # Client Secret
EBAY_REDIRECT_URI = os.environ.get("EBAY_REDIRECT_URI", "")  # RuName

# OAuth2 scopes needed for Inventory + Trading APIs
EBAY_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
]

# Token file path (persists across restarts)
EBAY_TOKEN_FILE = Path(__file__).parent / "ebay_tokens.json"

# Legacy fallback: static token from .env (used if OAuth not configured)
EBAY_TOKEN_LEGACY = os.environ.get("EBAY_TOKEN", "YOUR_EBAY_TOKEN_HERE")

# ── CSV Price Database ────────────────────────────────────────────────────────
# Map Gemini's full platform names → CSV abbreviations
PLATFORM_TO_CSV = {
    "nintendo 64": "N64", "n64": "N64",
    "super nintendo": "SNES", "super nes": "SNES", "snes": "SNES",
    "nintendo entertainment system": "NES", "nes": "NES",
    "game boy": "GameBoy", "gameboy": "GameBoy",
    "game boy color": "GameBoy Color", "gameboy color": "GameBoy Color",
    "game boy advance": "GameBoy Advance", "gameboy advance": "GameBoy Advance", "gba": "GameBoy Advance",
    "gamecube": "GameCube", "nintendo gamecube": "GameCube",
    "nintendo ds": "Nintendo DS", "ds": "Nintendo DS",
    "wii": "Wii", "nintendo wii": "Wii",
    "playstation": "PlayStation", "ps1": "PlayStation", "psx": "PlayStation", "playstation 1": "PlayStation",
    "playstation 2": "PlayStation 2", "ps2": "PlayStation 2",
    "playstation 3": "PlayStation 3", "ps3": "PlayStation 3",
    "xbox": "Xbox", "microsoft xbox": "Xbox",
    "xbox 360": "Xbox 360", "microsoft xbox 360": "Xbox 360",
}


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for fuzzy matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def _load_csv_prices() -> tuple[dict, dict]:
    """Load CSV into a lookup dict keyed by (csv_platform, normalized_title).
    Returns (exact_lookup, titles_by_platform) for fuzzy fallback."""
    csv_path = Path(__file__).parent / "pricecharting_master_price.csv"
    lookup = {}           # (platform, norm_title) → {loose, cib}
    titles_by_plat = {}   # platform → {norm_title: original_title}

    if not csv_path.exists():
        print("[csv] pricecharting_nintendo_prices.csv not found")
        return lookup, titles_by_plat

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            platform = row.get("Console", "").strip()
            title = row.get("Title", "").strip()
            if not platform or not title:
                continue
            norm = _normalize_title(title)
            # Parse prices: "$19.42" or "$1,750.00" → float
            loose = row.get("Loose Price", "$0").replace("$", "").replace(",", "")
            cib = row.get("CIB Price", "$0").replace("$", "").replace(",", "")
            try:
                loose_f = float(loose) if loose else 0.0
                cib_f = float(cib) if cib else 0.0
            except ValueError:
                continue
            lookup[(platform, norm)] = {"loose": loose_f, "cib": cib_f, "title": title}
            if platform not in titles_by_plat:
                titles_by_plat[platform] = {}
            titles_by_plat[platform][norm] = title

    print(f"[csv] Loaded {len(lookup)} game prices from CSV")
    return lookup, titles_by_plat


CSV_PRICES, CSV_TITLES_BY_PLATFORM = _load_csv_prices()


def _csv_lookup(game_title: str, platform: str) -> dict | None:
    """Look up a game in the CSV price database. Returns price dict or None."""
    # Map platform name to CSV format
    csv_platform = PLATFORM_TO_CSV.get(platform.lower(), platform)
    norm_title = _normalize_title(game_title)

    # Exact match
    match = CSV_PRICES.get((csv_platform, norm_title))
    if match:
        print(f"[csv] Exact match: '{game_title}' on {csv_platform} → ${match['loose']:.2f} loose, ${match['cib']:.2f} CIB")
        return {"loose_price": match["loose"], "cib_price": match["cib"], "new_price": 0, "source": "csv"}

    # Fuzzy match within the same platform
    platform_titles = CSV_TITLES_BY_PLATFORM.get(csv_platform, {})
    if not platform_titles:
        return None

    candidates = list(platform_titles.keys())
    close = difflib.get_close_matches(norm_title, candidates, n=1, cutoff=0.8)
    if close:
        matched_norm = close[0]
        match = CSV_PRICES[(csv_platform, matched_norm)]
        print(f"[csv] Fuzzy match: '{game_title}' → '{match['title']}' on {csv_platform} → ${match['loose']:.2f} loose, ${match['cib']:.2f} CIB")
        return {"loose_price": match["loose"], "cib_price": match["cib"], "new_price": 0, "source": "csv"}

    return None


def _load_token_data() -> dict:
    """Load saved OAuth tokens from disk."""
    if EBAY_TOKEN_FILE.exists():
        try:
            return json.loads(EBAY_TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_token_data(data: dict):
    """Persist OAuth tokens to disk."""
    EBAY_TOKEN_FILE.write_text(json.dumps(data, indent=2))


def _refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token. Returns updated token data."""
    token_url = f"{EBAY_BASE_URL}/identity/v1/oauth2/token"
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(EBAY_SCOPES),
    }
    resp = requests.post(token_url, headers=headers, data=body, timeout=15)
    resp.raise_for_status()
    token_resp = resp.json()

    token_data = _load_token_data()
    token_data["access_token"] = token_resp["access_token"]
    token_data["expires_at"] = time.time() + token_resp.get("expires_in", 7200) - 300  # 5 min buffer
    # refresh_token may be returned (rotated) or not
    if "refresh_token" in token_resp:
        token_data["refresh_token"] = token_resp["refresh_token"]
        token_data["refresh_expires_at"] = time.time() + token_resp.get("refresh_token_expires_in", 47304000)
    _save_token_data(token_data)
    print(f"[oauth] Access token refreshed, expires in {token_resp.get('expires_in', '?')}s")
    return token_data


def get_ebay_token() -> str:
    """Get a valid eBay access token. Auto-refreshes if expired. Falls back to legacy .env token."""
    token_data = _load_token_data()

    if token_data.get("access_token"):
        # Check if token is still valid (with 5 min buffer already baked into expires_at)
        if time.time() < token_data.get("expires_at", 0):
            return token_data["access_token"]

        # Token expired — try to refresh
        refresh_token = token_data.get("refresh_token")
        if refresh_token and EBAY_APP_ID and EBAY_CERT_ID:
            try:
                token_data = _refresh_access_token(refresh_token)
                return token_data["access_token"]
            except Exception as e:
                print(f"[oauth] Refresh failed: {e}")

    # Fallback to legacy static token from .env
    if EBAY_TOKEN_LEGACY and EBAY_TOKEN_LEGACY != "YOUR_EBAY_TOKEN_HERE":
        return EBAY_TOKEN_LEGACY

    return ""

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────

def _convert_heic_to_jpg(path: str) -> str:
    """If path is a HEIC/HEIF file, convert to JPEG and return new path. Otherwise return as-is."""
    if os.path.splitext(path)[1].lower() not in ('.heic', '.heif'):
        return path
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    jpg_path = os.path.splitext(path)[0] + '.jpg'
    img = Image.open(path)
    img.save(jpg_path, 'JPEG', quality=92)
    print(f"[heic] Converted {os.path.basename(path)} → {os.path.basename(jpg_path)}")
    return jpg_path


def _mime_for(path: str) -> str:
    """Return MIME type based on file extension (handles HEIC from iPhone)."""
    ext = os.path.splitext(path)[1].lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp"}.get(ext, "image/jpeg")


def upload_to_ebay_eps(image_path: str) -> str:
    """Upload image to eBay via Trading API UploadSiteHostedPictures, return ebayimg.com URL."""
    token = get_ebay_token()
    if not token:
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
            "image": (os.path.basename(image_path), image_data, _mime_for(image_path)),
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
    """Get market prices — tries local CSV first, then PriceCharting API."""
    # 1. Try local CSV database
    csv_result = _csv_lookup(game_title, platform)
    if csv_result:
        return csv_result

    # 2. Fall back to PriceCharting API
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
    # 3. Fallback: return zeroes, Cerebras will estimate
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

Price 5-10% below market to sell quickly. Condition options: NEW (sealed), LIKE_NEW, USED_EXCELLENT, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE."""

    response = cerebras.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json_response(response.choices[0].message.content)


def post_to_ebay(listing: dict, game_info: dict, image_urls: list = None) -> dict:
    """Post the listing to eBay via Inventory API."""
    token = get_ebay_token()
    print(f"[publish] Token loaded = {bool(token)}")
    if not token:
        return {"success": False, "error": "No eBay token configured. Connect eBay from the app.", "listing_id": None}

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

@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PIN:
        return redirect(url_for("index"))
    if request.method == "POST":
        if request.form.get("pin") == APP_PIN:
            session["authenticated"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Wrong PIN")
    return render_template("login.html", error=None)


@app.route("/ebay/auth")
@require_pin
def ebay_auth():
    """Start eBay OAuth2 flow — redirect user to eBay consent page."""
    if not EBAY_APP_ID or not EBAY_REDIRECT_URI:
        return jsonify({"error": "EBAY_APP_ID and EBAY_REDIRECT_URI must be set in .env"}), 500

    scope = " ".join(EBAY_SCOPES)
    auth_url = (
        f"{EBAY_AUTH_URL}/oauth2/authorize"
        f"?client_id={EBAY_APP_ID}"
        f"&response_type=code"
        f"&redirect_uri={EBAY_REDIRECT_URI}"
        f"&scope={requests.utils.quote(scope)}"
    )
    return redirect(auth_url)


@app.route("/ebay/callback")
@require_pin
def ebay_callback():
    """Handle eBay OAuth2 redirect — exchange auth code for tokens."""
    code = request.args.get("code")
    if not code:
        error = request.args.get("error_description", "Authorization denied or failed")
        return f"<h2>eBay Auth Error</h2><p>{error}</p><a href='/'>Back to app</a>", 400

    # Exchange authorization code for access + refresh tokens
    token_url = f"{EBAY_BASE_URL}/identity/v1/oauth2/token"
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": EBAY_REDIRECT_URI,
    }

    try:
        resp = requests.post(token_url, headers=headers, data=body, timeout=15)
        resp.raise_for_status()
        token_resp = resp.json()
    except Exception as e:
        return f"<h2>Token Exchange Failed</h2><pre>{e}</pre><a href='/'>Back to app</a>", 500

    # Save tokens to disk
    token_data = {
        "access_token": token_resp["access_token"],
        "expires_at": time.time() + token_resp.get("expires_in", 7200) - 300,
        "refresh_token": token_resp.get("refresh_token", ""),
        "refresh_expires_at": time.time() + token_resp.get("refresh_token_expires_in", 47304000),
        "token_type": token_resp.get("token_type", "User Access Token"),
        "connected_at": time.time(),
    }
    _save_token_data(token_data)
    print(f"[oauth] eBay connected! Access token expires in {token_resp.get('expires_in', '?')}s, "
          f"refresh token expires in {token_resp.get('refresh_token_expires_in', '?')}s")

    return redirect(url_for("index"))


@app.route("/ebay/status")
@require_pin
def ebay_status():
    """Return current eBay connection status as JSON."""
    token_data = _load_token_data()

    if token_data.get("access_token"):
        refresh_expires = token_data.get("refresh_expires_at", 0)
        connected = True
        # Check if refresh token is still valid
        if refresh_expires and time.time() > refresh_expires:
            connected = False
            status = "expired"
        elif time.time() > token_data.get("expires_at", 0):
            status = "needs_refresh"  # will auto-refresh on next API call
        else:
            status = "active"
        return jsonify({
            "connected": connected,
            "status": status,
            "oauth_configured": bool(EBAY_APP_ID and EBAY_CERT_ID and EBAY_REDIRECT_URI),
        })

    # Check legacy token
    has_legacy = EBAY_TOKEN_LEGACY and EBAY_TOKEN_LEGACY != "YOUR_EBAY_TOKEN_HERE"
    return jsonify({
        "connected": has_legacy,
        "status": "legacy" if has_legacy else "disconnected",
        "oauth_configured": bool(EBAY_APP_ID and EBAY_CERT_ID and EBAY_REDIRECT_URI),
    })


@app.route("/ebay/disconnect", methods=["POST"])
@require_pin
def ebay_disconnect():
    """Clear saved OAuth tokens."""
    if EBAY_TOKEN_FILE.exists():
        EBAY_TOKEN_FILE.unlink()
    return jsonify({"success": True})


@app.route("/")
@require_pin
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
@require_pin
def analyze():
    photos = request.files.getlist("photos")
    if not photos or not photos[0].filename:
        return jsonify({"error": "No photos uploaded"}), 400

    extra_notes = request.form.get("notes", "")

    # Save all uploaded files (convert HEIC→JPG for eBay compatibility)
    image_paths = []
    for i, photo in enumerate(photos):
        ext = Path(photo.filename).suffix or ".jpg"
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], f"upload_{i}{ext}")
        photo.save(save_path)
        save_path = _convert_heic_to_jpg(save_path)
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
@require_pin
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


@app.route("/analyze-batch", methods=["POST"])
@require_pin
def analyze_batch():
    """Analyze multiple games via SSE. Expects game_count, game_{n}_photos[], game_{n}_notes fields."""
    game_count = int(request.form.get("game_count", 0))
    if game_count < 1:
        return jsonify({"error": "No games in batch"}), 400

    # Pre-save ALL files before entering the generator (Flask request context closes after response starts)
    batch_id = str(int(time.time() * 1000))
    games_data = []
    for g in range(game_count):
        photos = request.files.getlist(f"game_{g}_photos")
        notes = request.form.get(f"game_{g}_notes", "")
        game_dir = os.path.join(app.config["UPLOAD_FOLDER"], batch_id, f"game_{g}")
        os.makedirs(game_dir, exist_ok=True)
        image_paths = []
        for i, photo in enumerate(photos):
            ext = Path(photo.filename).suffix or ".jpg"
            save_path = os.path.join(game_dir, f"photo_{i}{ext}")
            photo.save(save_path)
            save_path = _convert_heic_to_jpg(save_path)
            image_paths.append(save_path)
        games_data.append({"image_paths": image_paths, "notes": notes})

    def generate():
        for g, gd in enumerate(games_data):
            try:
                # Step 1: Identify
                yield f"data: {json.dumps({'type': 'progress', 'game': g, 'step': 'identifying'})}\n\n"
                game_info = analyze_game_photo(gd["image_paths"][0])

                # Step 2: Price
                yield f"data: {json.dumps({'type': 'progress', 'game': g, 'step': 'pricing'})}\n\n"
                price_info = get_market_price(game_info["game_title"], game_info["platform"])

                # Step 3: Generate listing
                yield f"data: {json.dumps({'type': 'progress', 'game': g, 'step': 'generating'})}\n\n"
                listing = generate_listing(game_info, price_info, gd["notes"])

                yield f"data: {json.dumps({'type': 'result', 'game': g, 'data': {'game_info': game_info, 'price_info': price_info, 'listing': listing, 'image_paths': gd['image_paths']}})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'game': g, 'error': str(e)})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/publish-batch", methods=["POST"])
@require_pin
def publish_batch():
    """Publish multiple games sequentially. Expects JSON {games: [{listing, game_info, image_paths}, ...]}."""
    data = request.json
    games = data.get("games", [])
    if not games:
        return jsonify({"error": "No games to publish"}), 400

    results = []
    for i, game in enumerate(games):
        listing = game.get("listing")
        game_info = game.get("game_info")
        image_paths = game.get("image_paths", [])

        # Upload images to eBay EPS
        image_urls = []
        for path in image_paths:
            if os.path.exists(path):
                url = upload_to_ebay_eps(path)
                if url:
                    image_urls.append(url)

        result = post_to_ebay(listing, game_info, image_urls=image_urls)
        result["game_index"] = i
        results.append(result)

    return jsonify({"results": results})


os.makedirs("uploads", exist_ok=True)

if __name__ == "__main__":
    # Debug: confirm env vars loaded
    print(f"[startup] EBAY_SANDBOX = {EBAY_SANDBOX}")
    print(f"[startup] EBAY_BASE_URL = {EBAY_BASE_URL}")
    print(f"[startup] EBAY_APP_ID set = {bool(EBAY_APP_ID)}")
    print(f"[startup] EBAY_REDIRECT_URI = {EBAY_REDIRECT_URI or '(not set)'}")
    token = get_ebay_token()
    print(f"[startup] eBay token available = {bool(token)}")
    token_data = _load_token_data()
    if token_data.get("access_token"):
        print(f"[startup] OAuth tokens on disk, refresh expires = {time.ctime(token_data.get('refresh_expires_at', 0))}")
    elif EBAY_TOKEN_LEGACY and EBAY_TOKEN_LEGACY != "YOUR_EBAY_TOKEN_HERE":
        print(f"[startup] Using legacy EBAY_TOKEN from .env")
    app.run(host="0.0.0.0", port=8080, debug=True)
