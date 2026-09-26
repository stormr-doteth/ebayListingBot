"""Mercari (US) cross-listing through browser automation.

Mercari has no public seller API, so this drives mercari.com in a real browser (Playwright) using
a saved, logged-in profile in `mercari_profile/` (gitignored). It runs on your Mac, from your
home connection, at a human pace.

One-time setup (on the Mac):
    ./venv/bin/pip install playwright && ./venv/bin/playwright install chromium
    ./venv/bin/python mercari.py login      # a browser opens — log in to Mercari, then press Enter here

Mercari changes its site now and then. If listing breaks:
    ./venv/bin/python mercari.py probe      # dumps the sell form's fields to mercari_debug/probe.json
and update SELECTORS below (every locator the code uses lives there).

Safety: MERCARI_SUBMIT is off by default — the form is filled and screenshotted but not submitted,
so you can check a few in mercari_debug/ before letting it list for real.
"""

import html
import os
import random
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from dotenv import load_dotenv

import inventory

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")  # config below is read at import time
PROFILE_DIR = BASE_DIR / "mercari_profile"
DEBUG_DIR = BASE_DIR / "mercari_debug"


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ── Config (.env) ─────────────────────────
SUBMIT = _env_bool("MERCARI_SUBMIT", False)            # False = fill the form, screenshot, don't list
HEADLESS = _env_bool("MERCARI_HEADLESS", True)         # False shows the browser window (handy for debugging)
CHANNEL = os.environ.get("MERCARI_CHANNEL", "chrome")  # "chrome" = your installed Google Chrome; "" = Playwright's Chromium
PRICE_MARKUP = float(os.environ.get("MERCARI_PRICE_MARKUP", "0"))  # % added to the eBay price (fees differ)
CATEGORY = os.environ.get("MERCARI_CATEGORY", "Electronics > Video Games & Consoles > Video Games")
SHIPPING = os.environ.get("MERCARI_SHIPPING", "")      # optional: text of the shipping option to pick
CHECK_INTERVAL = int(os.environ.get("MERCARI_CHECK_INTERVAL", "900"))   # sold check every 15 min
MIN_GAP = int(os.environ.get("MERCARI_MIN_GAP", "90"))  # seconds between two listings
DELIST_MODE = os.environ.get("MERCARI_DELIST", "deactivate")  # deactivate (reversible) | delete

SELL_URL = "https://www.mercari.com/sell/"
EDIT_URL = "https://www.mercari.com/sell/edit/{id}/"
LOGIN_URL = "https://www.mercari.com/login/"
SOLD_PAGES = [u.strip() for u in os.environ.get(
    "MERCARI_SOLD_PAGES",
    "https://www.mercari.com/mypage/listings/in_progress/,https://www.mercari.com/mypage/listings/complete/",
).split(",") if u.strip()]
ITEM_ID_RE = re.compile(r"/item/(m\d{6,})")

TITLE_MAX, DESC_MAX, PHOTO_MAX = 80, 1000, 12

# eBay condition → Mercari condition label
CONDITION_MAP = {
    "NEW": "New", "LIKE_NEW": "Like new", "USED_EXCELLENT": "Like new",
    "USED_VERY_GOOD": "Good", "USED_GOOD": "Good", "USED_ACCEPTABLE": "Fair",
}

# Every locator, with fallbacks tried in order. Prefixes: "label:" → get_by_label,
# "button:" → get_by_role("button", exact name), "text:" → get_by_text (exact), else CSS.
# These are best guesses from Mercari's web form — confirm with `python mercari.py probe`.
SELECTORS = {
    "photos": ['input[type="file"]'],
    "title": ['[data-testid="Title"] input', 'input[name="name"]', "label:Title",
              'input[placeholder*="What are you selling" i]'],
    "description": ['[data-testid="Description"] textarea', 'textarea[name="description"]', "label:Description",
                    "textarea"],
    "category_level": ['[data-testid="CategoryL{level}"]', '[data-testid="Category{level}"]'],
    "condition": ['[data-testid="Condition{value}"]', "text:{value}", "label:{value}"],
    "price": ['[data-testid="Price"] input', 'input[name="price"]', "label:Price",
              'input[placeholder*="price" i]'],
    "submit": ["button:List", '[data-testid="ListButton"]'],
    "deactivate": ["button:Deactivate", "text:Deactivate"],
    "delete": ["button:Delete", "button:Delete listing", "text:Delete listing"],
    "confirm": ["button:Deactivate", "button:Delete", "button:Yes", "button:Confirm", "button:OK"],
}

_browser_lock = threading.Lock()   # one browser session at a time
_last_listing = {"at": 0.0}
_photo_resolver = None             # set by app.py: photo ref → local Path (or None)


def set_photo_resolver(fn):
    global _photo_resolver
    _photo_resolver = fn


# ─────────────────────────────────────────
#  Browser helpers
# ─────────────────────────────────────────

@contextmanager
def browser(headless: bool | None = None):
    """A page in the logged-in Mercari profile. Serialized: only one browser at a time."""
    from playwright.sync_api import sync_playwright  # imported lazily so the app runs without it
    with _browser_lock, sync_playwright() as p:
        kwargs = dict(headless=HEADLESS if headless is None else headless, locale="en-US",
                      viewport={"width": 1280, "height": 900})
        if CHANNEL:
            kwargs["channel"] = CHANNEL
        try:
            ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
        except Exception:
            if not CHANNEL:
                raise
            kwargs.pop("channel")  # Chrome not installed → Playwright's Chromium
            ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            ctx.close()


def _pause(lo: float = 0.6, hi: float = 1.8):
    time.sleep(random.uniform(lo, hi))


def _locator(page, spec: str):
    if spec.startswith("label:"):
        return page.get_by_label(spec[6:])
    if spec.startswith("button:"):
        return page.get_by_role("button", name=spec[7:], exact=True)
    if spec.startswith("text:"):
        return page.get_by_text(spec[5:], exact=True)
    return page.locator(spec)


def _find(page, key: str, timeout: float = 8.0, **fmt):
    """First candidate for SELECTORS[key] that exists on the page (waits up to `timeout` seconds)."""
    specs = [s.format(**fmt) for s in SELECTORS[key]]
    deadline = time.time() + timeout
    while True:
        for spec in specs:
            loc = _locator(page, spec)
            try:
                if loc.count() and (key == "photos" or loc.first.is_visible()):
                    return loc.first
            except Exception:
                pass
        if time.time() > deadline:
            raise RuntimeError(f"Mercari page changed? Couldn't find the {key} field "
                               f"(tried {specs}) — run `python mercari.py probe`")
        time.sleep(0.5)


def _type(page, key: str, text: str):
    field = _find(page, key)
    field.click()
    field.fill("")
    if len(text) > 120:
        field.fill(text)       # long text: paste
    else:
        field.press_sequentially(text, delay=random.randint(25, 70))
    _pause(0.3, 0.9)


def _check_logged_in(page):
    if "/login" in page.url or "/signup" in page.url:
        raise inventory.NeedsLogin("Mercari session expired — run `python mercari.py login` on the Mac")


def _screenshot(page, name: str) -> str:
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        return ""
    return str(path.relative_to(BASE_DIR))


def html_to_text(desc: str) -> str:
    """eBay HTML description → plain text for Mercari."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", desc or "")
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def mercari_price(ebay_price: float) -> int:
    """Mercari takes whole dollars, $1 minimum."""
    return max(1, int(round(ebay_price * (1 + PRICE_MARKUP / 100))))


def _photo_files(item: dict, workdir: Path) -> list[str]:
    """Local copies of the item's photos: the original uploads if still around, else the eBay-hosted images."""
    files = []
    if _photo_resolver:
        files = [str(p) for p in (_photo_resolver(r) for r in item.get("photo_refs", [])) if p]
    if not files:
        for n, url in enumerate(item.get("image_urls", [])[:PHOTO_MAX]):
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            path = workdir / f"photo_{n}.jpg"
            path.write_bytes(resp.content)
            files.append(str(path))
    if not files:
        raise RuntimeError("No photos available for this item")
    return files[:PHOTO_MAX]


def _sold_page_ids(page) -> set[str]:
    """Every Mercari item ID linked from your in-progress / completed sales pages."""
    found: set[str] = set()
    for url in SOLD_PAGES:
        page.goto(url, wait_until="domcontentloaded")
        _pause(2, 4)
        _check_logged_in(page)
        for _ in range(5):  # scroll so lazy-loaded rows appear
            page.mouse.wheel(0, 4000)
            _pause(0.5, 1.0)
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        found |= {m.group(1) for h in hrefs if h for m in [ITEM_ID_RE.search(h)] if m}
    return found


# ─────────────────────────────────────────
#  Adapter
# ─────────────────────────────────────────

class MercariAdapter(inventory.Adapter):
    name = "mercari"
    label = "Mercari"
    can_list = True
    check_interval = CHECK_INTERVAL

    def status(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, "install Playwright (see mercari.py)"
        if not PROFILE_DIR.exists():
            return False, "not logged in — run `python mercari.py login`"
        return True, "ready" if SUBMIT else "dry run (MERCARI_SUBMIT=false)"

    def list_item(self, item: dict) -> dict:
        wait = MIN_GAP - (time.time() - _last_listing["at"])
        if wait > 0:
            time.sleep(wait + random.uniform(0, 20))
        title = item["title"][:TITLE_MAX].strip()
        description = html_to_text(item.get("description", ""))[:DESC_MAX]
        condition = CONDITION_MAP.get(item.get("condition", ""), "Good")
        price = mercari_price(item["price"])

        with tempfile.TemporaryDirectory() as tmp, browser() as page:
            photos = _photo_files(item, Path(tmp))
            try:
                page.goto(SELL_URL, wait_until="domcontentloaded")
                _pause(2, 4)
                _check_logged_in(page)

                _find(page, "photos").set_input_files(photos)
                _pause(2, 4)  # thumbnails upload
                _type(page, "title", title)
                _type(page, "description", description)
                self._set_category(page)
                _find(page, "condition", value=condition).click()
                _pause()
                if SHIPPING:
                    page.get_by_text(SHIPPING, exact=False).first.click()
                    _pause()
                _type(page, "price", str(price))
                shot = _screenshot(page, f"{item['sku']}-filled")

                if not SUBMIT:
                    return {"note": f"dry run — form filled, not submitted. Check {shot}, then set "
                                    f"MERCARI_SUBMIT=true to list for real"}

                _find(page, "submit").click()
                page.wait_for_url(re.compile(r"/item/m\d+"), timeout=60000)
                listing_id = ITEM_ID_RE.search(page.url).group(1)
                _last_listing["at"] = time.time()
                return {"listing_id": listing_id, "url": f"https://www.mercari.com/us/item/{listing_id}/"}
            except inventory.NeedsLogin:
                raise
            except Exception as e:
                shot = _screenshot(page, f"{item['sku']}-error")
                raise RuntimeError(f"{e} (screenshot: {shot})") from e

    def _set_category(self, page):
        levels = [c.strip() for c in CATEGORY.split(">") if c.strip()]
        for level, name in enumerate(levels):
            try:
                _find(page, "category_level", timeout=4, level=level).click()
            except RuntimeError:
                if level == 0:
                    # No dropdowns — Mercari may show category suggestions after the title instead
                    page.get_by_text(levels[-1], exact=False).first.click()
                    _pause()
                    return
                raise
            _pause(0.4, 1.0)
            # exact: "Video Games" must not match "Video Games & Consoles"; last = the list that just opened
            page.get_by_role("option", name=name, exact=True).or_(page.get_by_text(name, exact=True)).last.click()
            _pause(0.5, 1.2)

    def sold_listing_ids(self, listings: list[dict]) -> set[str]:
        wanted = {l["listing_id"] for l in listings if l.get("listing_id")}
        with browser() as page:
            return _sold_page_ids(page) & wanted

    def delist(self, listing: dict):
        with browser() as page:
            page.goto(EDIT_URL.format(id=listing["listing_id"]), wait_until="domcontentloaded")
            _pause(2, 3)
            _check_logged_in(page)
            try:
                _find(page, "delete" if DELIST_MODE == "delete" else "deactivate").click()
                _pause()
                try:  # most removals ask "are you sure?"
                    page.get_by_role("dialog").get_by_role("button").filter(
                        has_text=re.compile("deactivate|delete|yes|confirm|ok", re.I)).first.click(timeout=5000)
                except Exception:
                    pass
                _pause(2, 3)
            except Exception as e:
                shot = _screenshot(page, f"{listing['sku']}-delist-error")
                raise RuntimeError(f"{e} (screenshot: {shot})") from e


# ─────────────────────────────────────────
#  Command line: login / probe / sold
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
    import json
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        with browser(headless=False) as page:
            page.goto(LOGIN_URL)
            input("Log in to Mercari in the browser window, then press Enter here… ")
        print(f"Saved the Mercari login in {PROFILE_DIR.name}/")
    elif cmd == "probe":
        with browser(headless=False) as page:
            page.goto(SELL_URL, wait_until="domcontentloaded")
            _pause(3, 4)
            _check_logged_in(page)
            input("Open any dropdowns you want captured (e.g. category), then press Enter… ")
            fields = page.evaluate(PROBE_JS)
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / "probe.json").write_text(json.dumps(fields, indent=2))
            print(f"{len(fields)} elements → mercari_debug/probe.json, screenshot → {_screenshot(page, 'probe')}")
    elif cmd == "sold":
        with browser() as page:
            found = _sold_page_ids(page)
        print(f"Item IDs on your sold/in-progress pages: {sorted(found) or 'none'}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
