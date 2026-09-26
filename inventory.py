"""Cross-listing inventory for RetroList.

One row per game you own (keyed by the eBay SKU), plus one row per marketplace it's listed on.
The sync loop asks every marketplace "what sold?" and, when a game sells anywhere, takes it down
everywhere else. Marketplaces plug in as adapters (see `Adapter`): eBay lives in app.py (it needs
the eBay login), Mercari in mercari.py.

Storage is a single SQLite file (inventory.db, gitignored). Every function opens its own
connection, so it's safe to call from Flask request threads and the sync thread at once.
"""

import json
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DB_PATH = Path(__file__).parent / "inventory.db"

# Listing statuses
LISTED = "listed"              # live on that marketplace
PENDING = "pending"            # listing job queued / running
ERROR = "error"                # listing attempt failed (see `error`)
SOLD = "sold"                  # this is the marketplace it sold on
DELISTED = "delisted"          # taken down because it sold elsewhere
DELIST_FAILED = "delist_failed"  # still up somewhere even though it sold — retried every sync, shown in red

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    sku          TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    price        REAL NOT NULL,
    condition    TEXT,
    description  TEXT,           -- eBay HTML description
    game_info    TEXT,           -- JSON from the Claude analysis (may be {} for imported items)
    photo_refs   TEXT,           -- JSON list of local upload refs (deleted after UPLOAD_RETENTION_DAYS)
    image_urls   TEXT,           -- JSON list of permanent ebayimg.com URLs
    status       TEXT NOT NULL DEFAULT 'active',   -- active | sold
    sold_on      TEXT,           -- platform name, or 'manual'
    sold_at      REAL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    sku          TEXT NOT NULL REFERENCES items(sku),
    platform     TEXT NOT NULL,
    listing_id   TEXT,
    offer_id     TEXT,           -- eBay only
    url          TEXT,
    status       TEXT NOT NULL,
    error        TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (sku, platform)
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    level    TEXT NOT NULL,      -- info | sold | warn | error
    sku      TEXT,
    message  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)


# ─────────────────────────────────────────
#  Items, listings, events
# ─────────────────────────────────────────

def add_item(sku: str, title: str, price: float, condition: str = "", description: str = "",
             game_info: dict | None = None, photo_refs: list | None = None, image_urls: list | None = None):
    """Record a game. Re-adding an existing SKU updates its details but never its sold status."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO items (sku, title, price, condition, description, game_info, photo_refs, image_urls, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sku) DO UPDATE SET title=excluded.title, price=excluded.price,
                   condition=excluded.condition, description=excluded.description,
                   game_info=excluded.game_info, image_urls=excluded.image_urls""",
            (sku, title, float(price), condition, description, json.dumps(game_info or {}),
             json.dumps(photo_refs or []), json.dumps(image_urls or []), time.time()))


def set_listing(sku: str, platform: str, status: str, listing_id: str | None = None,
                offer_id: str | None = None, url: str | None = None, error: str | None = None):
    """Create or update a listing row. None leaves an existing listing_id / offer_id / url alone."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO listings (sku, platform, listing_id, offer_id, url, status, error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sku, platform) DO UPDATE SET
                   listing_id=COALESCE(excluded.listing_id, listing_id),
                   offer_id=COALESCE(excluded.offer_id, offer_id),
                   url=COALESCE(excluded.url, url),
                   status=excluded.status, error=excluded.error, updated_at=excluded.updated_at""",
            (sku, platform, listing_id, offer_id, url, status, error, time.time()))


def log_event(level: str, message: str, sku: str | None = None):
    print(f"[inventory] {level.upper()}: {message}")
    with _connect() as conn:
        conn.execute("INSERT INTO events (at, level, sku, message) VALUES (?, ?, ?, ?)",
                     (time.time(), level, sku, message))


def _item_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("game_info", "photo_refs", "image_urls"):
        item[key] = json.loads(item[key] or ("{}" if key == "game_info" else "[]"))
    return item


def get_item(sku: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
        if not row:
            return None
        item = _item_dict(row)
        item["listings"] = {r["platform"]: dict(r) for r in
                            conn.execute("SELECT * FROM listings WHERE sku = ?", (sku,))}
    return item


def all_items() -> list[dict]:
    """Every item with its listings, newest first."""
    with _connect() as conn:
        items = [_item_dict(r) for r in conn.execute("SELECT * FROM items ORDER BY created_at DESC")]
        listings: dict[str, dict] = {}
        for r in conn.execute("SELECT * FROM listings"):
            listings.setdefault(r["sku"], {})[r["platform"]] = dict(r)
    for item in items:
        item["listings"] = listings.get(item["sku"], {})
    return items


def listings_with_status(platform: str, *statuses: str) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM listings WHERE platform = ? AND status IN ({marks})", (platform, *statuses))]


def recent_events(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def get_meta(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str):
    with _connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ─────────────────────────────────────────
#  Marketplace adapters
# ─────────────────────────────────────────

class NeedsLogin(Exception):
    """The marketplace session is logged out — the user has to log in again."""


class Adapter:
    """One marketplace. Subclasses override what they support.

    - `list_item(item)` → {"listing_id", "url", optional "offer_id"}; raise on failure.
    - `delist(listing)` takes the listing down; raise on failure. Must be safe to call twice.
    - `sold_listing_ids(listings)` → the listing_ids (out of the given live listings) that sold.
    """
    name = "base"
    label = "Base"
    can_list = False           # does "List on <label>" make sense in the UI?
    check_interval = 300       # seconds between sold checks

    def status(self) -> tuple[bool, str]:
        """(usable right now, short message for the UI)."""
        return True, "ready"

    def list_item(self, item: dict) -> dict:
        raise NotImplementedError

    def delist(self, listing: dict):
        raise NotImplementedError

    def sold_listing_ids(self, listings: list[dict]) -> set[str]:
        return set()


ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter):
    ADAPTERS[adapter.name] = adapter


# ─────────────────────────────────────────
#  Selling and delisting
# ─────────────────────────────────────────

_sale_lock = threading.Lock()   # one sale is processed at a time, so two can't race each other


def mark_sold(sku: str, platform: str) -> list[str]:
    """A game sold on `platform` (or 'manual'). Take it down everywhere else.
    Returns the platforms where delisting failed (they're retried every sync)."""
    with _sale_lock:
        item = get_item(sku)
        if not item:
            log_event("warn", f"Sale on {platform} for unknown SKU {sku}", sku)
            return []
        if item["status"] == "sold":
            if item["sold_on"] != platform and platform in item["listings"]:
                # Two buyers got it on two sites before we could delist — you need to cancel one.
                set_listing(sku, platform, SOLD)
                log_event("error", f"DOUBLE SALE: \"{item['title']}\" sold on {item['sold_on']} AND {platform} "
                                   f"— cancel one of the orders", sku)
            return []

        with _connect() as conn:
            conn.execute("UPDATE items SET status='sold', sold_on=?, sold_at=? WHERE sku=?",
                         (platform, time.time(), sku))
        if platform in item["listings"]:
            set_listing(sku, platform, SOLD)
        log_event("sold", f"\"{item['title']}\" sold on {platform}", sku)

        failed = []
        for other, listing in item["listings"].items():
            if other != platform and listing["status"] in (LISTED, DELIST_FAILED, PENDING):
                if not _delist(listing, item["title"]):
                    failed.append(other)
        return failed


def _delist(listing: dict, title: str) -> bool:
    adapter = ADAPTERS.get(listing["platform"])
    sku, platform = listing["sku"], listing["platform"]
    if listing["status"] == PENDING or not listing.get("listing_id"):
        # Still being listed — the listing job checks the sold status when it finishes.
        return True
    try:
        if not adapter:
            raise RuntimeError(f"no adapter for {platform}")
        adapter.delist(listing)
        set_listing(sku, platform, DELISTED)
        log_event("info", f"Removed \"{title}\" from {platform}", sku)
        return True
    except Exception as e:
        set_listing(sku, platform, DELIST_FAILED, error=str(e)[:500])
        log_event("error", f"Couldn't remove \"{title}\" from {platform} — remove it by hand if this keeps "
                           f"failing: {e}", sku)
        return False


# Listing jobs run one at a time in the background (browser automation is slow and must not overlap)
_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="list-job")


def queue_listing(sku: str, platform: str) -> str:
    """Start listing an item on a marketplace in the background. Returns an error message or ''."""
    adapter = ADAPTERS.get(platform)
    item = get_item(sku)
    if not adapter or not adapter.can_list:
        return f"Can't list on {platform} from here"
    if not item:
        return "Unknown item"
    if item["status"] != "active":
        return "That game already sold"
    current = item["listings"].get(platform, {}).get("status")
    if current in (LISTED, PENDING):
        return f"Already {current} on {adapter.label}"
    ok, msg = adapter.status()
    if not ok:
        return f"{adapter.label}: {msg}"
    set_listing(sku, platform, PENDING, error=None)
    _jobs.submit(_run_listing, sku, platform)
    return ""


def _run_listing(sku: str, platform: str):
    adapter = ADAPTERS[platform]
    item = get_item(sku)
    try:
        result = adapter.list_item(item)
    except Exception as e:
        traceback.print_exc()
        set_listing(sku, platform, ERROR, error=str(e)[:500])
        log_event("error", f"Listing \"{item['title']}\" on {adapter.label} failed: {e}", sku)
        return
    if not result.get("listing_id"):  # dry run: form filled, not submitted
        set_listing(sku, platform, ERROR, error=result.get("note") or "Not submitted")
        log_event("info", f"{adapter.label} dry run for \"{item['title']}\": {result.get('note', '')}", sku)
        return
    set_listing(sku, platform, LISTED, listing_id=str(result["listing_id"]), offer_id=result.get("offer_id"),
                url=result.get("url"), error=None)
    log_event("info", f"Listed \"{item['title']}\" on {adapter.label}", sku)
    # It may have sold elsewhere while we were filling in the form — take the new listing straight down.
    if get_item(sku)["status"] == "sold":
        _delist(get_item(sku)["listings"][platform], item["title"])


# ─────────────────────────────────────────
#  Sync loop
# ─────────────────────────────────────────

_last_checked: dict[str, float] = {}
_sync_lock = threading.Lock()
sync_state = {"last_run": 0.0, "running": False, "errors": {}}


def sync_once(force: bool = False):
    """Ask each marketplace what sold, process the sales, retry failed delists."""
    if not _sync_lock.acquire(blocking=False):
        return  # a sync is already running
    sync_state["running"] = True
    try:
        for name, adapter in ADAPTERS.items():
            if not force and time.time() - _last_checked.get(name, 0) < adapter.check_interval:
                continue
            # DELIST_FAILED too: if one of those shows up as sold, it's a double sale we must report
            live = listings_with_status(name, LISTED, DELIST_FAILED)
            ok, msg = adapter.status()
            if not live:
                _last_checked[name] = time.time()
                sync_state["errors"].pop(name, None)
                continue
            if not ok:
                sync_state["errors"][name] = msg
                continue
            try:
                sold = adapter.sold_listing_ids(live)
                _last_checked[name] = time.time()
                sync_state["errors"].pop(name, None)
            except NeedsLogin as e:
                sync_state["errors"][name] = f"log in again ({e})"
                continue
            except Exception as e:
                traceback.print_exc()
                sync_state["errors"][name] = str(e)[:300]
                continue
            for listing in live:
                if listing["listing_id"] in sold:
                    mark_sold(listing["sku"], name)

        # Anything that sold but is still up somewhere: try again
        for name in ADAPTERS:
            for listing in listings_with_status(name, DELIST_FAILED):
                item = get_item(listing["sku"])
                _delist(listing, item["title"] if item else listing["sku"])
        sync_state["last_run"] = time.time()
    finally:
        sync_state["running"] = False
        _sync_lock.release()


def start_sync_thread(interval: int = 60):
    """Wake up every `interval` seconds; each adapter is only checked every `check_interval`."""
    def loop():
        while True:
            try:
                sync_once()
            except Exception:
                traceback.print_exc()
            time.sleep(interval)
    threading.Thread(target=loop, name="inventory-sync", daemon=True).start()
