"""
Cross-listing for RetroList: the inventory database and the Mercari publisher.

- Inventory (SQLite, inventory.db): one row per physical item, plus one row per marketplace
  listing of it. This is what lets the sync loop in app.py end every other listing when an
  item sells somewhere, so a one-of-a-kind game can't sell twice.
- Mercari has no public seller API, so listings are made by driving a real browser (Playwright)
  with a profile you log into once on this Mac. All browser work runs on ONE worker thread
  (Playwright's sync API isn't thread-safe) and jobs queue up behind each other.

Mercari's page layout changes now and then. Every selector lives in MERCARI_UI below, and
MERCARI_DRY_RUN (on by default) fills the form, takes a screenshot and stops before "List",
so you can check it works before anything goes live.
"""
import html
import json
import os
import random
import re
import sys
import tempfile
import sqlite3
import threading
import time
import uuid
import queue
from concurrent.futures import Future
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")  # before the settings below are read (app.py imports this module early)


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────
#  Inventory database
# ─────────────────────────────────────────

DB_PATH = BASE_DIR / "inventory.db"
MARKETS = ("ebay", "mercari")

# Listing statuses:
#   queued     waiting for the Mercari worker
#   listed     live
#   draft      Mercari test mode: form filled + screenshot, not posted
#   failed     publish failed (error says why) — can be retried
#   sold       sold on this marketplace
#   ended      we ended it because the item sold elsewhere
#   end_failed couldn't end it automatically — retried every sync, and shown in red until it's gone
#   double_sold it sold here too before it could be ended — cancel one of the two orders!
LIVE = ("listed",)
_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, _db() as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items (
                id          TEXT PRIMARY KEY,
                created_at  REAL NOT NULL,
                title       TEXT NOT NULL,
                price       REAL NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active',   -- active | sold
                sold_on     TEXT,
                sold_at     REAL,
                data        TEXT NOT NULL                     -- {listing, game_info, photos}
            );
            CREATE TABLE IF NOT EXISTS listings (
                item_id     TEXT NOT NULL REFERENCES items(id),
                market      TEXT NOT NULL,
                status      TEXT NOT NULL,
                external_id TEXT,        -- eBay listing id / Mercari item id (m123...)
                sku         TEXT,        -- eBay SKU (matches Fulfillment API orders)
                offer_id    TEXT,        -- eBay offer id (needed to end the listing)
                url         TEXT,
                price       REAL,
                error       TEXT,
                screenshot  TEXT,        -- Mercari test-mode screenshot (file name)
                updated_at  REAL NOT NULL,
                PRIMARY KEY (item_id, market)
            );
        """)


def create_item(listing: dict, game_info: dict, photos: list[str]) -> str:
    item_id = uuid.uuid4().hex[:12]
    with _db_lock, _db() as conn:
        conn.execute("INSERT INTO items (id, created_at, title, price, data) VALUES (?, ?, ?, ?, ?)",
                     (item_id, time.time(), listing.get("title", ""), float(listing.get("suggested_price") or 0),
                      json.dumps({"listing": listing, "game_info": game_info, "photos": photos})))
    return item_id


def update_item_data(item_id: str, listing: dict, game_info: dict, photos: list[str]):
    """A retry may carry edits (new price, fixed title) — keep the latest version (and the saved
    eBay image URLs, which the browser never sends)."""
    data = {**item_data(item_id), "listing": listing, "game_info": game_info, "photos": photos}
    with _db_lock, _db() as conn:
        conn.execute("UPDATE items SET title = ?, price = ?, data = ? WHERE id = ?",
                     (listing.get("title", ""), float(listing.get("suggested_price") or 0),
                      json.dumps(data), item_id))


def get_item(item_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def set_listing(item_id: str, market: str, **fields):
    """Insert or update one marketplace listing row (only the given fields change)."""
    fields["updated_at"] = time.time()
    with _db_lock, _db() as conn:
        exists = conn.execute("SELECT 1 FROM listings WHERE item_id = ? AND market = ?",
                              (item_id, market)).fetchone()
        if exists:
            cols = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE listings SET {cols} WHERE item_id = ? AND market = ?",
                         (*fields.values(), item_id, market))
        else:
            fields.setdefault("status", "queued")
            cols = ", ".join(["item_id", "market", *fields])
            conn.execute(f"INSERT INTO listings ({cols}) VALUES ({', '.join('?' * (len(fields) + 2))})",
                         (item_id, market, *fields.values()))


def get_listings(item_id: str) -> dict[str, dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM listings WHERE item_id = ?", (item_id,)).fetchall()
    return {r["market"]: dict(r) for r in rows}


def watched_listings(market: str) -> list[dict]:
    """Listings to check for sales: live ones, plus ones we failed to end (a sale there = double sale)."""
    with _db() as conn:
        rows = conn.execute("""SELECT l.*, i.created_at FROM listings l JOIN items i ON i.id = l.item_id
                               WHERE l.market = ? AND (l.status = 'end_failed'
                                     OR (l.status = 'listed' AND i.status = 'active'))""", (market,)).fetchall()
    return [dict(r) for r in rows]


def end_failed_listings() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM listings WHERE status = 'end_failed'").fetchall()
    return [dict(r) for r in rows]


def known_ebay_skus() -> set[str]:
    with _db() as conn:
        rows = conn.execute("SELECT sku FROM listings WHERE market = 'ebay' AND sku IS NOT NULL").fetchall()
    return {r["sku"] for r in rows}


def item_data(item_id: str) -> dict:
    """{listing, game_info, photos, image_urls} as stored for an item."""
    item = get_item(item_id)
    return json.loads(item["data"]) if item else {}


def set_item_data_field(item_id: str, key: str, value):
    data = item_data(item_id)
    data[key] = value
    with _db_lock, _db() as conn:
        conn.execute("UPDATE items SET data = ? WHERE id = ?", (json.dumps(data), item_id))


def create_imported_item(title: str, price: float, data: dict) -> str:
    item_id = uuid.uuid4().hex[:12]
    with _db_lock, _db() as conn:
        conn.execute("INSERT INTO items (id, created_at, title, price, data) VALUES (?, ?, ?, ?, ?)",
                     (item_id, time.time(), title, price, json.dumps(data)))
    return item_id


def live_listings(market: str) -> list[dict]:
    """Live listings on a marketplace, for items that haven't sold anywhere yet."""
    with _db() as conn:
        rows = conn.execute("""SELECT l.*, i.created_at FROM listings l JOIN items i ON i.id = l.item_id
                               WHERE l.market = ? AND l.status = 'listed' AND i.status = 'active'""",
                            (market,)).fetchall()
    return [dict(r) for r in rows]


def mark_sold(item_id: str, market: str) -> list[dict]:
    """Record a sale. Returns the item's OTHER live listings, which now need ending.
    If the item had already sold on another marketplace, this is a double sale: flagged, nothing returned."""
    with _db_lock, _db() as conn:
        item = conn.execute("SELECT status, sold_on FROM items WHERE id = ?", (item_id,)).fetchone()
        if item and item["status"] == "sold" and item["sold_on"] != market:
            conn.execute("UPDATE listings SET status = 'double_sold', error = ?, updated_at = ? "
                         "WHERE item_id = ? AND market = ?",
                         (f"DOUBLE SALE — also sold on {item['sold_on']}. Cancel one of the orders.",
                          time.time(), item_id, market))
            return []
        conn.execute("UPDATE items SET status = 'sold', sold_on = ?, sold_at = ? WHERE id = ? AND status = 'active'",
                     (market, time.time(), item_id))
        conn.execute("UPDATE listings SET status = 'sold', updated_at = ? WHERE item_id = ? AND market = ?",
                     (time.time(), item_id, market))
        rows = conn.execute("SELECT * FROM listings WHERE item_id = ? AND market != ? AND status IN ('listed', 'queued')",
                            (item_id, market)).fetchall()
    return [dict(r) for r in rows]


def item_summaries(item_ids: list[str] | None = None, limit: int = 100) -> list[dict]:
    """Items with their listings, newest first — for the UI."""
    with _db() as conn:
        if item_ids:
            marks = ",".join("?" * len(item_ids))
            items = conn.execute(f"SELECT * FROM items WHERE id IN ({marks})", item_ids).fetchall()
        else:
            items = conn.execute("SELECT * FROM items ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for it in items:
            rows = conn.execute("SELECT * FROM listings WHERE item_id = ?", (it["id"],)).fetchall()
            out.append({
                "id": it["id"], "title": it["title"], "price": it["price"], "status": it["status"],
                "sold_on": it["sold_on"], "created_at": it["created_at"],
                "listings": {r["market"]: {k: r[k] for k in ("status", "url", "price", "error", "screenshot",
                                                             "external_id")} for r in rows},
                "imported": not json.loads(it["data"]).get("game_info", {}).get("game_title"),
            })
    return out


def needs_attention() -> list[dict]:
    """Listings that must be ended by hand (automatic ending failed after a sale)."""
    with _db() as conn:
        rows = conn.execute("""SELECT l.market, l.url, l.status, i.title, i.sold_on FROM listings l
                               JOIN items i ON i.id = l.item_id
                               WHERE l.status IN ('end_failed', 'double_sold')""").fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────
#  Mercari: turning a RetroList listing into Mercari's form values
# ─────────────────────────────────────────

MERCARI_DRY_RUN = _env_bool("MERCARI_DRY_RUN", True)        # fill + screenshot, don't click List
MERCARI_HEADLESS = _env_bool("MERCARI_HEADLESS", False)     # a real window is less likely to be blocked
MERCARI_OFFSCREEN = _env_bool("MERCARI_OFFSCREEN", True)    # ...but park it off-screen so it doesn't pop up
MERCARI_CHANNEL = os.environ.get("MERCARI_BROWSER", "chrome")  # "chrome" = your installed Google Chrome
MERCARI_MARKUP = float(os.environ.get("MERCARI_PRICE_MARKUP", "0"))  # % added to the eBay price
MERCARI_PROFILE = BASE_DIR / "mercari_profile"               # browser profile (your Mercari login)
MERCARI_LOGIN_STATE = MERCARI_PROFILE / "login_state.json"   # cookies + localStorage (see _save_login)
MERCARI_MIN_GAP = int(os.environ.get("MERCARI_MIN_GAP", "90"))  # seconds between two listings — human pace
MERCARI_SHOTS = BASE_DIR / "uploads" / "mercari_screens"
MERCARI_TITLE_MAX = 80
MERCARI_DESC_MAX = 1000
MERCARI_PHOTO_MAX = 12
MERCARI_HOME = "https://www.mercari.com"

# RetroList condition → Mercari's five conditions
MERCARI_CONDITIONS = {
    "NEW": "New", "LIKE_NEW": "Like new", "USED_EXCELLENT": "Like new",
    "USED_VERY_GOOD": "Good", "USED_GOOD": "Good", "USED_ACCEPTABLE": "Fair",
}

# Platform → brand buyers filter by on Mercari
PLATFORM_BRANDS = [("nintendo", "Nintendo"), ("game boy", "Nintendo"), ("gamecube", "Nintendo"),
                   ("wii", "Nintendo"), ("n64", "Nintendo"), ("snes", "Nintendo"), ("nes", "Nintendo"),
                   ("ds", "Nintendo"), ("playstation", "Sony"), ("ps", "Sony"), ("xbox", "Microsoft"),
                   ("sega", "Sega"), ("genesis", "Sega"), ("dreamcast", "Sega"), ("atari", "Atari")]


def html_to_text(markup: str) -> str:
    """eBay's simple HTML description → Mercari plain text (bullets kept as '• ')."""
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", markup or "")
    text = re.sub(r"(?i)</p>|<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</ul>|</ol>", "\n\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def mercari_price(price: float) -> int:
    """Mercari prices are whole dollars, $1 minimum."""
    return max(1, int(round(float(price) * (1 + MERCARI_MARKUP / 100))))


def to_mercari(listing: dict, game_info: dict) -> dict:
    """Map a RetroList listing to Mercari form values."""
    title = re.sub(r"\s+", " ", listing.get("title") or game_info.get("game_title", "")).strip()
    if len(title) > MERCARI_TITLE_MAX:
        title = title[:MERCARI_TITLE_MAX].rsplit(" ", 1)[0]

    # Mercari has no item specifics, so the useful ones go at the end of the description
    specifics = [f"{label}: {game_info[key]}" for key, label in
                 (("platform", "Platform"), ("region", "Region"), ("mpn", "Part number"), ("year", "Year"))
                 if str(game_info.get(key) or "").strip().lower() not in ("", "n/a", "unknown", "other")]
    description = html_to_text(listing.get("description", ""))
    if specifics:
        description += "\n\n" + "\n".join(specifics)
    if len(description) > MERCARI_DESC_MAX:
        description = description[:MERCARI_DESC_MAX - 1].rsplit("\n", 1)[0]

    platform = (game_info.get("platform") or "").lower()
    brand = next((b for key, b in PLATFORM_BRANDS if re.search(rf"\b{key}", platform)), "")
    return {
        "title": title,
        "description": description,
        "condition": MERCARI_CONDITIONS.get(listing.get("condition"), "Good"),
        "brand": brand,
        "price": mercari_price(listing.get("suggested_price") or 0),
        "weight_oz": float(listing.get("weight_oz") or 8),
    }


# ─────────────────────────────────────────
#  Mercari: the page, as selectors
# ─────────────────────────────────────────
# Each entry is a list of alternatives, tried in order — the first one found on the page wins.
# If Mercari changes its layout, the test-mode screenshot + warnings show which step broke;
# fix it here. ("text=…" / "role=…" are Playwright selector syntax.)

MERCARI_UI = {
    "sell_url": f"{MERCARI_HOME}/sell/",
    "login_url": f"{MERCARI_HOME}/login/",
    "photo_input": ['input[type="file"][accept*="image"]', 'input[type="file"]'],
    "title": ['[data-testid="Title"]', 'input[name="name"]', 'input[placeholder*="title" i]',
              'input[aria-label*="title" i]'],
    "description": ['[data-testid="Description"]', 'textarea[name="description"]', 'textarea'],
    "category_path": ["Electronics", "Video Games & Consoles", "Video Games"],
    "category_open": ['[data-testid="CategoryL0"]', 'button:has-text("Category")', 'text=Select category'],
    "brand": ['[data-testid="Brand"]', 'input[placeholder*="brand" i]', 'input[aria-label*="brand" i]'],
    "condition_group": ['[data-testid="Condition"]', 'fieldset:has-text("Condition")'],
    "price": ['[data-testid="Price"]', 'input[name="price"]', 'input[placeholder*="price" i]',
              'input[aria-label*="price" i]'],
    "list_button": ['[data-testid="ListButton"]', 'button:text-is("List")', 'role=button[name="List"]'],
    # After "List", Mercari opens the new item's page: /us/item/m12345678901/
    "item_url_re": r"/item/(m\d{6,})",
    # Account pages that list sold items (the sync loop reads item ids from their links)
    "sold_pages": [f"{MERCARI_HOME}/mypage/listings/in_progress/", f"{MERCARI_HOME}/mypage/listings/complete/"],
    "logged_in_check": f"{MERCARI_HOME}/mypage/listings/active/",
    # Ending a listing: item page → Edit listing → Deactivate
    "edit_button": ['[data-testid="EditButton"]', 'role=button[name=/edit listing/i]', 'text=Edit listing'],
    "deactivate_button": ['[data-testid="DeactivateButton"]', 'role=button[name=/deactivate/i]', 'text=Deactivate'],
    "confirm_button": ['role=dialog >> role=button[name=/deactivate|yes|confirm/i]'],
}


class MercariError(RuntimeError):
    pass


def _pause(lo: float = 0.4, hi: float = 1.2):
    """Short random pause between form steps — typing at machine speed looks like a bot."""
    time.sleep(random.uniform(lo, hi))


def _save_login(ctx):
    """Save cookies + localStorage. A persistent profile alone drops Mercari's session cookies
    when the browser closes, which would log you out every time."""
    try:
        MERCARI_PROFILE.mkdir(exist_ok=True)
        ctx.storage_state(path=str(MERCARI_LOGIN_STATE))
        MERCARI_LOGIN_STATE.chmod(0o600)
    except Exception as e:
        print(f"[mercari] Couldn't save login: {e}")


def _restore_login(ctx):
    if not MERCARI_LOGIN_STATE.exists():
        return
    try:
        state = json.loads(MERCARI_LOGIN_STATE.read_text())
    except (ValueError, OSError):
        return
    if state.get("cookies"):
        ctx.add_cookies(state["cookies"])
    for origin in state.get("origins", []):
        items = {i["name"]: i["value"] for i in origin.get("localStorage", [])}
        if items:
            ctx.add_init_script(
                "(([origin, items]) => { if (location.origin !== origin) return;"
                " for (const [k, v] of Object.entries(items)) if (localStorage.getItem(k) === null)"
                " localStorage.setItem(k, v); })(" + json.dumps([origin["origin"], items]) + ")")


def _first(page, selectors: list[str], timeout: int = 4000):
    """The first selector that matches a visible element, or None."""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.25)
    return None


def _first_attached(page, selectors: list[str], timeout: int = 6000):
    """Like _first, but for hidden elements (file inputs are usually invisible)."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="attached", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


# ─────────────────────────────────────────
#  Mercari: the browser worker
# ─────────────────────────────────────────

class MercariWorker(threading.Thread):
    """Owns the browser. Everything that touches Mercari goes through submit() and runs here,
    one job at a time. The browser starts on first use and closes after a minute of no jobs."""

    IDLE_CLOSE_SECONDS = 60

    def __init__(self):
        super().__init__(daemon=True, name="mercari")
        self.jobs: queue.Queue = queue.Queue()
        self._pw = None
        self._ctx = None
        self.logged_in: bool | None = None   # None = not checked yet
        self.busy = False
        self._last_listing = 0.0

    # ── plumbing ──
    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        self.jobs.put((fn, args, fut))
        return fut

    def run(self):
        while True:
            try:
                fn, args, fut = self.jobs.get(timeout=self.IDLE_CLOSE_SECONDS)
            except queue.Empty:
                self._close()
                continue
            self.busy = True
            try:
                fut.set_result(fn(*args))
            except Exception as e:
                print(f"[mercari] {fn.__name__} failed: {e}")
                fut.set_exception(e)
            finally:
                if self._ctx and self.logged_in:
                    _save_login(self._ctx)
                self.busy = False

    def _context(self, headless: bool | None = None, interactive: bool = False):
        if self._ctx:
            return self._ctx
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise MercariError("Playwright isn't installed — run: ./venv/bin/pip install playwright && "
                               "./venv/bin/playwright install chromium")
        self._pw = sync_playwright().start()
        headless = MERCARI_HEADLESS if headless is None else headless
        visible = headless is False and interactive   # the login window must be on screen
        opts = dict(user_data_dir=str(MERCARI_PROFILE), headless=headless,
                    viewport={"width": 1280, "height": 900}, locale="en-US",
                    args=[] if visible or headless or not MERCARI_OFFSCREEN else ["--window-position=-3000,0"])
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(channel=MERCARI_CHANNEL or None, **opts)
        except Exception as e:  # Google Chrome not installed → Playwright's bundled Chromium
            print(f"[mercari] Couldn't start {MERCARI_CHANNEL!r} ({e}); using bundled Chromium")
            self._ctx = self._pw.chromium.launch_persistent_context(**opts)
        _restore_login(self._ctx)
        return self._ctx

    def _close(self):
        if self._ctx:
            if self.logged_in:  # Mercari refreshes its tokens — keep the newest (never save a logged-out state)
                _save_login(self._ctx)
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._ctx = self._pw = None

    def _page(self):
        return self._context().new_page()

    # ── jobs (run on the worker thread) ──
    def job_check_login(self) -> bool:
        page = self._page()
        try:
            page.goto(MERCARI_UI["logged_in_check"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            self.logged_in = "/login" not in page.url
            return self.logged_in
        finally:
            page.close()

    def job_open_login(self) -> str:
        """Open a visible window on the Mac at Mercari's login page. You log in there once;
        the profile keeps you logged in. Closes on its own after 5 minutes or once logged in."""
        self._close()
        page = self._context(headless=False, interactive=True).new_page()
        page.goto(MERCARI_UI["login_url"], wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + 300
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if page.is_closed():
                break
            if "/login" not in page.url and "/signup" not in page.url:
                self.logged_in = True
                break
        try:
            page.close()
        except Exception:
            pass
        self._close()  # reopen later with the normal headless setting
        return "logged_in" if self.logged_in else "timed_out"

    def job_publish(self, data: dict, photo_paths: list[str], shot_name: str, dry_run: bool,
                    image_urls: list[str] | None = None) -> dict:
        """Fill Mercari's sell form. Returns {status, url?, external_id?, warnings, screenshot}.
        photo_paths are local photos; if there are none (older than the upload cleanup, or an imported
        eBay listing), the eBay-hosted image_urls are downloaded instead."""
        wait = MERCARI_MIN_GAP - (time.time() - self._last_listing)
        if wait > 0 and not dry_run:
            print(f"[mercari] Waiting {int(wait)}s between listings (MERCARI_MIN_GAP)")
            time.sleep(wait)
        tmp = None
        if not photo_paths and image_urls:
            tmp = tempfile.TemporaryDirectory()
            photo_paths = []
            for n, url in enumerate(image_urls[:MERCARI_PHOTO_MAX]):
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                path = Path(tmp.name) / f"photo_{n}.jpg"
                path.write_bytes(resp.content)
                photo_paths.append(str(path))
        if not photo_paths:
            raise MercariError("No photos for this item")
        page = self._page()
        warnings: list[str] = []
        try:
            page.goto(MERCARI_UI["sell_url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if "/login" in page.url:
                self.logged_in = False
                raise MercariError("Not logged in to Mercari — tap “Log in to Mercari” in the app")
            self.logged_in = True

            # Photos
            photo_input = _first_attached(page, MERCARI_UI["photo_input"])
            if not photo_input:
                raise MercariError("Couldn't find the photo upload on Mercari's sell page")
            photo_input.set_input_files(photo_paths[:MERCARI_PHOTO_MAX])
            page.wait_for_timeout(1500 + 700 * min(len(photo_paths), MERCARI_PHOTO_MAX))

            # Title + description (typing the title also makes Mercari suggest a category)
            self._fill(page, "title", data["title"], warnings, required=True)
            self._fill(page, "description", data["description"], warnings, required=True)

            self._pick_category(page, warnings)
            if data.get("brand"):
                self._pick_brand(page, data["brand"], warnings)
            self._pick_condition(page, data["condition"], warnings)
            self._fill(page, "price", str(int(data["price"])), warnings, required=True)
            # Shipping: Mercari defaults to its prepaid label and suggests a service from the
            # category — review it in the test-mode screenshot before going live.

            page.wait_for_timeout(1000)
            MERCARI_SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(MERCARI_SHOTS / shot_name), full_page=True)

            if dry_run:
                return {"status": "draft", "warnings": warnings, "screenshot": shot_name}
            self._last_listing = time.time()

            button = _first(page, MERCARI_UI["list_button"])
            if not button:
                raise MercariError("Couldn't find Mercari's List button")
            if not button.is_enabled():
                raise MercariError("Mercari's List button is disabled — a required field is missing "
                                   f"({'; '.join(warnings) or 'see screenshot'})")
            button.click()
            try:
                page.wait_for_url(re.compile(MERCARI_UI["item_url_re"]), timeout=45000)
            except Exception:
                page.screenshot(path=str(MERCARI_SHOTS / shot_name), full_page=True)
                raise MercariError("Tapped List but Mercari didn't open the new listing — check the screenshot")
            item_id = re.search(MERCARI_UI["item_url_re"], page.url).group(1)
            return {"status": "listed", "external_id": item_id, "url": f"{MERCARI_HOME}/us/item/{item_id}/",
                    "warnings": warnings, "screenshot": shot_name}
        except MercariError:
            raise
        except Exception as e:
            try:
                MERCARI_SHOTS.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(MERCARI_SHOTS / shot_name), full_page=True)
            except Exception:
                pass
            raise MercariError(f"Mercari form error: {str(e).splitlines()[0][:300]}")
        finally:
            page.close()
            if tmp:
                tmp.cleanup()

    def job_sold_ids(self) -> set[str]:
        """Mercari item ids that have sold (in progress or completed orders)."""
        ids: set[str] = set()
        page = self._page()
        try:
            for url in MERCARI_UI["sold_pages"]:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
                if "/login" in page.url:
                    self.logged_in = False
                    raise MercariError("Not logged in to Mercari")
                for _ in range(4):  # these pages load more as you scroll
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(800)
                ids.update(re.findall(MERCARI_UI["item_url_re"], page.content()))
            self.logged_in = True
            return ids
        finally:
            page.close()

    def job_deactivate(self, url: str) -> bool:
        """End a listing without deleting it (it can be reactivated from Mercari)."""
        page = self._page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            edit = _first(page, MERCARI_UI["edit_button"], timeout=8000)
            if not edit:
                raise MercariError("No Edit button on the item page")
            edit.click()
            page.wait_for_timeout(2500)
            deactivate = _first(page, MERCARI_UI["deactivate_button"], timeout=8000)
            if not deactivate:
                raise MercariError("No Deactivate button on the edit page")
            deactivate.click()
            confirm = _first(page, MERCARI_UI["confirm_button"], timeout=3000)
            if confirm:
                confirm.click()
            page.wait_for_timeout(2500)
            return True
        finally:
            page.close()

    # ── form helpers ──
    def _fill(self, page, key: str, value: str, warnings: list, required: bool = False):
        field = _first(page, MERCARI_UI[key])
        if not field:
            msg = f"Couldn't find the {key} field"
            if required:
                raise MercariError(msg)
            warnings.append(msg)
            return
        field.click()
        field.fill(value)
        _pause()

    def _pick_category(self, page, warnings: list):
        leaf = MERCARI_UI["category_path"][-1]
        page.wait_for_timeout(1500)
        # 1. Mercari usually suggests categories after the title — take "Video Games" if offered
        suggestion = _first(page, [f'button:has-text("{leaf}")', f'label:has-text("{leaf}")'], timeout=2500)
        if suggestion:
            suggestion.click()
            return
        # 2. Otherwise open the picker and walk the path
        opener = _first(page, MERCARI_UI["category_open"], timeout=2500)
        if not opener:
            warnings.append("Couldn't find the category picker — set Video Games by hand")
            return
        opener.click()
        for name in MERCARI_UI["category_path"]:
            option = _first(page, [f'role=option[name="{name}"]', f'role=button[name="{name}"]',
                                   f'li:text-is("{name}")', f'text="{name}"'], timeout=3000)
            if not option:
                warnings.append(f"Category step “{name}” not found — set the category by hand")
                return
            option.click()
            page.wait_for_timeout(600)

    def _pick_brand(self, page, brand: str, warnings: list):
        field = _first(page, MERCARI_UI["brand"], timeout=2000)
        if not field:
            warnings.append("No brand field (optional)")
            return
        field.click()
        field.fill(brand)
        option = _first(page, [f'role=option[name="{brand}"]', f'li:text-is("{brand}")'], timeout=3000)
        if option:
            option.click()
        else:
            warnings.append(f"Brand “{brand}” not offered (optional)")

    def _pick_condition(self, page, condition: str, warnings: list):
        scope = _first(page, MERCARI_UI["condition_group"], timeout=2000) or page
        option = None
        for sel in (f'role=radio[name=/^{re.escape(condition)}/i]', f'label:has-text("{condition}")',
                    f'text="{condition}"'):
            try:
                loc = scope.locator(sel).first
                if loc.count() and loc.is_visible():
                    option = loc
                    break
            except Exception:
                continue
        if option:
            option.click()
        else:
            warnings.append(f"Couldn't select condition “{condition}” — check it")


# ─────────────────────────────────────────
#  Public Mercari API for app.py
# ─────────────────────────────────────────

_worker: MercariWorker | None = None


def worker() -> MercariWorker:
    global _worker
    if _worker is None:
        _worker = MercariWorker()
        _worker.start()
    return _worker


def queue_publish(item_id: str, listing: dict, game_info: dict, photo_paths: list[Path],
                  image_urls: list[str] | None = None):
    """Queue a Mercari listing; the inventory row is updated when it finishes."""
    data = to_mercari(listing, game_info)
    set_listing(item_id, "mercari", status="queued", price=data["price"], error=None)
    shot = f"{item_id}.png"

    def done(fut: Future):
        try:
            r = fut.result()
            set_listing(item_id, "mercari", status=r["status"], external_id=r.get("external_id"),
                        url=r.get("url"), screenshot=r.get("screenshot"),
                        error="; ".join(r.get("warnings") or []) or None)
            print(f"[mercari] {data['title']!r} → {r['status']} {r.get('url') or ''}")
        except Exception as e:
            has_shot = (MERCARI_SHOTS / shot).exists()
            set_listing(item_id, "mercari", status="failed", error=str(e)[:500],
                        screenshot=shot if has_shot else None)

    worker().submit(worker().job_publish, data, [str(p) for p in photo_paths], shot, MERCARI_DRY_RUN,
                    image_urls or []).add_done_callback(done)


def status() -> dict:
    w = worker()
    return {"logged_in": w.logged_in, "busy": w.busy, "queued": w.jobs.qsize(),
            "dry_run": MERCARI_DRY_RUN, "markup": MERCARI_MARKUP}


def check_login(timeout: int = 90) -> bool:
    return worker().submit(worker().job_check_login).result(timeout=timeout)


def open_login():
    worker().submit(worker().job_open_login)


def sold_ids(timeout: int = 180) -> set[str]:
    return worker().submit(worker().job_sold_ids).result(timeout=timeout)


def deactivate(url: str, timeout: int = 120) -> bool:
    return worker().submit(worker().job_deactivate, url).result(timeout=timeout)


# ─────────────────────────────────────────
#  Command line (run in Terminal on the Mac):
#    ./venv/bin/python crosslist.py login   log in to Mercari in a browser window (saved for the app)
#    ./venv/bin/python crosslist.py check   still logged in?
#    ./venv/bin/python crosslist.py probe   dump the sell form's fields → uploads/mercari_screens/probe.json
#    ./venv/bin/python crosslist.py sold    item ids on your Mercari sold / in-progress pages
# ─────────────────────────────────────────

PROBE_JS = """() => [...document.querySelectorAll(
  'input, textarea, select, button, [role=button], [role=combobox], [role=radio], [data-testid]')]
  .filter(e => e.offsetParent !== null || e.type === 'file')
  .map(e => ({
    tag: e.tagName.toLowerCase(), type: e.type || null, name: e.name || null, id: e.id || null,
    testid: e.dataset.testid || null, placeholder: e.placeholder || null,
    aria: e.getAttribute('aria-label'), role: e.getAttribute('role'),
    label: e.labels && e.labels[0] ? e.labels[0].innerText.trim().slice(0, 60) : null,
    text: (e.innerText || '').trim().slice(0, 60) || null }))"""


def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    w = MercariWorker()  # used directly on this thread (not started)
    try:
        if cmd == "login":
            page = w._context(headless=False, interactive=True).new_page()
            page.goto(MERCARI_UI["login_url"])
            input("Log in to Mercari in the browser window, then press Enter here… ")
            page.close()
            ok = w.job_check_login()
            print("✅ Logged in — the app will use this login" if ok else
                  "❌ Mercari still shows the login page — run this again and finish logging in first")
        elif cmd == "check":
            print("✅ Logged in to Mercari" if w.job_check_login() else
                  "❌ Logged out — run: ./venv/bin/python crosslist.py login")
        elif cmd == "probe":
            page = w._context(headless=False, interactive=True).new_page()
            page.goto(MERCARI_UI["sell_url"])
            input("On the sell page: log in if asked, open the Category picker so its options show, "
                  "then press Enter here… ")
            w.logged_in = "/login" not in page.url
            fields = page.evaluate(PROBE_JS)
            MERCARI_SHOTS.mkdir(parents=True, exist_ok=True)
            (MERCARI_SHOTS / "probe.json").write_text(json.dumps(fields, indent=2))
            page.screenshot(path=str(MERCARI_SHOTS / "probe.png"), full_page=True)
            print(f"✅ {len(fields)} fields → uploads/mercari_screens/probe.json (+ probe.png)")
        elif cmd == "sold":
            print("Sold / in-progress item ids:", sorted(w.job_sold_ids()) or "none")
        else:
            print("Usage: ./venv/bin/python crosslist.py login | check | probe | sold")
    finally:
        w._close()


if __name__ == "__main__":
    _cli()
