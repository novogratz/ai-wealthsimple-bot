#!/usr/bin/env python3
"""
Wealthsimple browser automation via Playwright.

Default behavior stops at the review page. Passing --confirm submits a real order.
"""
import argparse
import json
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
AUTH = DATA / "ws_auth.json"
PROFILE_DIR = DATA / "browser_profile"  # persistent browser profile — keeps device trust
KEEPALIVE_LOCK = DATA / "ws_busy.lock"  # held during buy/sell so keepalive backs off
WS_HOME = "https://my.wealthsimple.com/app/home"
CDP_URL = "http://localhost:9222"

def find_browser_executable() -> str:
    """Return an installed Chrome/Edge executable on Windows, macOS, or Linux."""
    system = platform.system()
    candidates = {
        "Darwin": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ],
        "Windows": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
        "Linux": ["google-chrome", "microsoft-edge", "chromium", "chromium-browser"],
    }.get(system, [])
    for candidate in candidates:
        resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if resolved:
            return resolved
    raise RuntimeError(
        "No supported browser found. Install Google Chrome or Microsoft Edge, then retry."
    )


SELECT_ALL = "Meta+A" if platform.system() == "Darwin" else "Control+A"

DATA.mkdir(exist_ok=True)


def _acquire_busy_lock() -> None:
    KEEPALIVE_LOCK.write_text("busy")


def _release_busy_lock() -> None:
    KEEPALIVE_LOCK.unlink(missing_ok=True)


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Fallback for Windows consoles that don't support some Unicode chars (like \u2212)
        safe = msg.encode("ascii", errors="replace").decode("ascii")
        print(safe, flush=True)


def snap(page, tag: str) -> Path:
    path = DATA / f"screen_{tag}.png"
    page.screenshot(path=str(path))
    safe_print(f"  [snap] {path.name}")
    return path


def strip_exchange(symbol: str) -> str:
    return re.sub(r"\.(TO|V|CN|NE)$", "", symbol, flags=re.IGNORECASE)


def first_visible(page, selectors: list[str], timeout: int = 3000):
    from playwright.sync_api import TimeoutError as PWTimeout

    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout)
            return el
        except PWTimeout:
            continue
    return None


def click_first(page, selectors: list[str], timeout: int = 3000) -> bool:
    el = first_visible(page, selectors, timeout)
    if el:
        el.click()
        return True
    return False


def fill_visible_input(page, index: int, value: str, timeout: int = 3000) -> None:
    """Fill the nth visible input on the active Wealthsimple order ticket."""
    el = page.locator("input:visible").nth(index)
    el.wait_for(state="visible", timeout=timeout)
    box = el.bounding_box()
    if box:
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.keyboard.press(SELECT_ALL)
        page.keyboard.type(value, delay=50)
        return

    el.fill(value, timeout=timeout, force=True)


def fill_order_quantity(page, shares: int) -> None:
    try:
        dollars_toggle = page.locator('button[aria-description*="Currently Dollars"]').first
        if dollars_toggle.is_visible(timeout=1000):
            dollars_toggle.click()
            page.wait_for_timeout(400)
    except Exception:
        pass

    label = page.get_by_text("Shares", exact=True).last
    label.wait_for(state="visible", timeout=3000)
    box = label.bounding_box()
    if not box:
        raise RuntimeError("Shares label found but no bounding box was available.")

    # The input is horizontally aligned to the right of the Shares label.
    page.mouse.click(box["x"] + 190, box["y"] + box["height"] / 2)
    page.keyboard.press(SELECT_ALL)
    page.keyboard.type(str(shares), delay=50)
    page.wait_for_timeout(500)


def click_account_row_by_balance(page, balance_text: str) -> bool:
    text = page.get_by_text(balance_text, exact=False).first
    text.wait_for(state="visible", timeout=2000)
    box = text.bounding_box()
    if not box:
        return False
    page.mouse.click(1212, box["y"] + box["height"] / 2)
    return True


def switch_buy_in_to_dollars(page) -> None:
    try:
        shares_toggle = page.locator('button[aria-description*="Currently Shares"]').first
        if shares_toggle.is_visible(timeout=1000):
            shares_toggle.click()
            page.wait_for_timeout(400)
            return
    except Exception:
        pass
    try:
        if page.get_by_text("Shares", exact=True).last.is_visible(timeout=500):
            page.get_by_text("Shares", exact=True).last.click(timeout=1500)
            page.wait_for_timeout(400)
    except Exception:
        pass


def use_max_dollars(page) -> None:
    switch_buy_in_to_dollars(page)
    print("Clicking Max...")
    # Use a real Playwright click (not JS) so React state updates properly
    try:
        btn = page.get_by_text("Max", exact=True).first
        btn.wait_for(state="visible", timeout=4000)
        btn.click()
        page.wait_for_timeout(800)
    except Exception:
        raise RuntimeError("Max button not found — account may not be selected yet")
    # Verify the estimated cost updated to something non-zero — if it's still $0 the
    # account has no available cash (pending orders may be locking funds).
    try:
        cost_text = page.locator("body").inner_text(timeout=2000)
        if "Estimated cost" in cost_text:
            import re as _re
            m = _re.search(r"Estimated cost\s*\$([0-9,.]+)", cost_text)
            if m and float(m.group(1).replace(",", "")) < 0.01:
                raise RuntimeError(
                    "Estimated cost is $0 after Max — account has no available cash. "
                    "Cancel pending orders on Wealthsimple and retry."
                )
    except RuntimeError:
        raise
    except Exception:
        pass


def use_max_shares(page) -> None:
    print("Using Max shares / sell all...")
    clicked = page.evaluate("""
        () => {
            const labels = ['Max', 'Sell all', 'Sell All'];
            const els = [...document.querySelectorAll('button, [role="button"]')];
            for (const label of labels) {
                const btn = els.find(el => el.textContent.trim() === label);
                if (btn) { btn.click(); return true; }
            }
            return false;
        }
    """)
    if not clicked:
        raise RuntimeError("Max/Sell-all button not found — account may not be selected")
    page.wait_for_timeout(700)


def cancel_pending_on_stock_page(page) -> int:
    """
    Cancel any pending sell orders visible on the current WS stock trade page.
    WS shows a clickable 'X open order(s)' banner when pending orders exist.
    Returns number of orders cancelled (0 if none found — safe to call always).
    """
    cancelled = 0
    try:
        page.wait_for_timeout(600)
        # WS shows pending orders as a banner: "1 open order" / "open order" / "pending"
        banner_texts = ["open order", "pending order", "Open order", "Pending order"]
        for text in banner_texts:
            try:
                banner = page.locator(f':text("{text}")').first
                if not banner.is_visible(timeout=400):
                    continue
                safe_print(f"  Pending order banner found: '{text}' — attempting cancel...")
                banner.click()
                page.wait_for_timeout(1500)
                snap(page, "pending_order_opened")
                # Look for a Cancel button in the panel/modal that opened
                for cancel_sel in [
                    'button:has-text("Cancel order")',
                    'button:has-text("Cancel Order")',
                    '[aria-label*="cancel" i]',
                ]:
                    try:
                        btn = page.locator(cancel_sel).last
                        if btn.is_visible(timeout=800):
                            btn.click()
                            page.wait_for_timeout(1500)
                            # Confirm cancel dialog if it appears
                            for conf_sel in [
                                'button:has-text("Yes, cancel")',
                                'button:has-text("Yes")',
                                'button:has-text("Confirm")',
                            ]:
                                try:
                                    conf = page.locator(conf_sel).first
                                    if conf.is_visible(timeout=800):
                                        conf.click()
                                        page.wait_for_timeout(1000)
                                        break
                                except Exception:
                                    pass
                            snap(page, "order_cancelled")
                            print("  Pending order cancelled OK")
                            cancelled += 1
                            break
                    except Exception:
                        pass
                break
            except Exception:
                pass
    except Exception:
        pass
    return cancelled


def wait_before_close(page, keep_open: bool) -> None:
    if keep_open:
        print("Browser left open. Close it manually when you are done reviewing.")
        if sys.stdin.isatty():
            input("Press ENTER here to close it...")
        else:
            while True:
                page.wait_for_timeout(60_000)
    elif sys.stdin.isatty():
        input("Press ENTER to close the browser...")
    else:
        page.wait_for_timeout(5000)


def parse_money(value: str) -> float | None:
    cleaned = re.sub(r"[^0-9.\-]", "", value)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def read_position_from_trade_page(page, symbol: str) -> dict | None:
    """
    Navigate to the stock's trade page and read the actual position details.
    Uses navigate_to_stock to ensure we land on the USD/NASDAQ page, not TSX.
    Returns dict with fill_price (average cost), fill_quantity, fill_value.
    Handles both English and French WS UI labels.
    """
    try:
        page = navigate_to_stock(page, symbol)
        page.wait_for_timeout(1500)
        text = page.locator("body").inner_text(timeout=5000)

        # Log a snippet so we can debug pattern mismatches
        safe_print(f"  [position_read] page snippet: {text[200:600].replace(chr(10), ' ')}")

        # ── Share count ───────────────────────────────────────────────────────
        qty = None
        qty_patterns = [
            # English
            r"You\s+(?:own|have)\s+([0-9,.]+)\s+shares?",
            r"([0-9,.]+)\s+shares?\s+(?:owned|held|in\s+your\s+account)",
            # French WS labels
            r"Vous\s+(?:poss[eé]dez|avez|d[eé]tenez)\s+([0-9,.]+)\s+(?:action|titre|part)",
            r"([0-9,.]+)\s+actions?\s+(?:d[eé]tenues|en\s+portefeuille)",
            # Generic: a number followed by "share(s)" / "action(s)" near cost info
            r"\b([0-9]+(?:\.[0-9]+)?)\s+(?:shares?|actions?)\b",
        ]
        for pat in qty_patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                val = parse_money(m.group(1))
                if val and val > 0:
                    qty = val
                    break

        if not qty or qty <= 0:
            safe_print(f"  [position_read] share count not found for {symbol}")
            return None

        # ── Average cost per share (the only reliable entry price anchor) ─────
        # DO NOT use Market value / Valeur marchande — those drift with the price.
        price = None
        cost_patterns = [
            # English: "Average cost $4.65" / "Avg. cost $4.65"
            r"(?:Average|Avg\.?)\s*(?:cost|price)\s*[:\s]*\$?\s*([0-9,.]+)",
            # French: "Coût moyen $4.65" / "Prix moyen $4.65"
            r"(?:Co[uû]t|Prix)\s+moyen\s*[:\s]*\$?\s*([0-9,.]+)",
            # Book value (total) — divide by qty
            r"(?:Book\s*value|Valeur\s*comptable)\s*[:\s]*\$?\s*([0-9,.]+)",
            # Cost basis / base de coût
            r"(?:Cost\s*basis|Base\s*de\s*co[uû]t)\s*[:\s]*\$?\s*([0-9,.]+)",
        ]
        for pat in cost_patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                val = parse_money(m.group(1))
                if val and val > 0:
                    is_total = any(k in pat.lower() for k in ["book", "comptable", "basis", "base de"])
                    if is_total and val > qty * 2:
                        price = round(val / qty, 4)
                    else:
                        price = val
                    break

        if not price or price <= 0:
            safe_print(f"  [position_read] avg cost not found for {symbol} (qty={qty})")
            return None

        safe_print(f"  [position_read] {symbol}: {qty} sh @ ${price:.4f}")
        return {
            "fill_price": round(price, 4),
            "fill_quantity": qty,
            "fill_value": round(qty * price, 2),
        }
    except Exception as e:
        safe_print(f"  [position_read] Error: {e}")
        return None


def get_ws_price(symbol: str, shares: float | None = None) -> float | None:
    """
    Fetch the live current price for symbol from Wealthsimple.
    Works during overnight/Blue Ocean ATS sessions that Yahoo Finance misses.

    Strategy: use the WS search bar — the result card shows the live market value
    of any held position. Price = market_value / shares. No page navigation needed.
    Falls back to navigating the security-details page for unowned symbols.
    Returns None if browser not running or price cannot be parsed.
    """
    from playwright.sync_api import sync_playwright
    sym = symbol.upper()
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception:
                return None  # browser not running — degrade gracefully
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Ensure we're on WS home (session active)
            if WS_HOME not in page.url:
                page.goto(WS_HOME, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(2000)

            # Open search: force-click button (a command-surface overlay intercepts normal clicks)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            search_btn = first_visible(page, [
                '[aria-label*="Search" i]',
                '[placeholder*="Search" i]',
                'input[type="search"]',
                '[data-testid*="search"]',
            ], timeout=3000)
            if search_btn is None:
                return None
            search_btn.click(force=True, timeout=5000)
            page.wait_for_timeout(400)

            # Type into the active text input
            inp = first_visible(page, [
                'input[type="text"]',
                'input[type="search"]',
                'input[placeholder]',
            ], timeout=3000)
            if inp is None:
                return None
            inp.type(sym, delay=80)
            page.wait_for_timeout(2000)

            # Read the search results text — result cards include market value for held positions
            results_text = page.locator("body").inner_text(timeout=5000)

            # Dismiss search (Escape) to restore page state
            page.keyboard.press("Escape")

            # Navigate to the security-details page — shows live price including AH/PM.
            # The search-card market value (portfolio total / shares) is stale at close
            # and is NOT reliable during extended hours.
            link = page.locator('a[href*="security-details"]').first
            try:
                href = link.get_attribute("href", timeout=1000)
                if href:
                    page.goto(f"https://my.wealthsimple.com{href}" if href.startswith("/") else href,
                              wait_until="domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(2500)
                    sec_text = page.locator("body").inner_text(timeout=5000)
                    page.goto(WS_HOME, wait_until="domcontentloaded", timeout=15_000)
                    for pat in [r"\$([0-9]+\.[0-9]{2,4})\s*USD", r"([0-9]+\.[0-9]{2,4})\s*USD"]:
                        m2 = re.search(pat, sec_text, re.IGNORECASE)
                        if m2:
                            price = parse_money(m2.group(1))
                            if price and price > 0.01:
                                return price
            except Exception:
                pass

            return None
    except Exception as e:
        safe_print(f"  [ws_price] {sym}: {e}")
        return None


def parse_review_details(page, side: str, submitted: bool, review_text: str = "") -> dict:
    text = review_text or page.locator("body").inner_text(timeout=5000)

    def find_number(patterns: list[str]) -> float | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return parse_money(match.group(1))
        return None

    quantity = find_number([
        r"Estimated quantity\s+([0-9,.]+)",
        r"Quantity\s+([0-9,.]+)",
    ])
    value = find_number([
        r"Estimated cost\s+(?:\$0(?:\.00)?\s+fees\s+)?\$?([0-9,.]+)\s*USD",
        r"Estimated cost\s+\$?([0-9,.]+)",
        r"Estimated proceeds\s+\$?([0-9,.]+)",
        r"Estimated value\s+\$?([0-9,.]+)",
        r"Total\s+\$?([0-9,.]+)",
    ])

    account = None
    account_match = re.search(r"Account\s+([^\n]+)", text, flags=re.IGNORECASE)
    if account_match:
        account = account_match.group(1).strip()

    order_type = None
    order_match = re.search(r"Order type\s+([^\n]+)", text, flags=re.IGNORECASE)
    if order_match:
        order_type = order_match.group(1).strip()

    # Try to find current balance if visible on the page
    balance = None
    balance_match = re.search(r"Available to trade\s+\$?([0-9,.]+)", text, flags=re.IGNORECASE)
    if balance_match:
        balance = parse_money(balance_match.group(1))

    return {
        "side": side,
        "submitted": submitted,
        "estimated_quantity": quantity,
        "estimated_value": value,
        "account": account,
        "order_type": order_type,
        "balance": balance,
    }


def page_has_order_submitted_signal(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        return False

    success_phrases = [
        "order submitted",
        "order placed",
        "order received",
        "order queued",
        "buy order submitted",
        "sell order submitted",
        "your order has been submitted",
        "your order was submitted",
    ]
    if any(phrase in text for phrase in success_phrases):
        return True

    review_phrases = [
        "review order",
        "estimated cost",
        "estimated proceeds",
        "submit order",
        "place order",
        "queue order",
    ]
    if any(phrase in text for phrase in review_phrases):
        return False

    return False


def get_live_balance(page) -> float | None:
    print("Fetching live balance...")
    page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(6000)
    snap(page, "balance_check_home")

    # Find the highest USD balance across all Non-registered accounts on the home page
    text = page.locator("body").inner_text()

    # Strategy 1: Find all Non-registered/Unregistered/Personal lines with a USD amount,
    # then return the largest USD value (the account we'll trade from)
    try:
        best_usd = 0.0
        for m in re.finditer(
            r"(Non-registered|Unregistered|Personal)[^\n]*?\$([0-9,.]+)\s*USD",
            text, flags=re.IGNORECASE
        ):
            val = parse_money(m.group(2))
            if val is not None and val > best_usd:
                best_usd = val
        if best_usd > 0:
            print(f"  Found best Non-registered USD balance: ${best_usd:.2f}")
            return best_usd
    except Exception:
        pass

    # Do not treat CAD as spendable USD. Wealthsimple's options ticket requires
    # USD cash and otherwise stops at an explicit conversion/funding prompt.
    for pattern in [
        r"Available to trade\s+\$?([0-9,.]+)",
    ]:
        match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            val = parse_money(match.group(1))
            if val is not None and val > 0:
                print(f"  Found explicitly available balance: ${val:.2f}")
                return val

    # The home page often hides per-currency balances. Open a harmless SPY
    # option draft, read the account picker, then leave without clicking Next.
    try:
        print("  USD balance hidden on home — reading options account picker...")
        page = navigate_to_spy_options(page)
        clicked = page.evaluate(r"""
            () => {
                const strikes = [...document.querySelectorAll('*')].filter(el =>
                    el.children.length === 0 && /^\$[0-9]{3,4}$/.test((el.textContent || '').trim())
                );
                for (const strike of strikes) {
                    let row = strike.parentElement;
                    for (let i = 0; i < 8 && row; i++, row = row.parentElement) {
                        const prices = [...row.querySelectorAll('button')].filter(b =>
                            /^\$[0-9]/.test((b.textContent || '').trim())
                        );
                        if (prices.length >= 2) {
                            const ask = parseFloat((prices[prices.length - 1].textContent || '').replace('$', ''));
                            if (ask >= 0.10 && ask <= 0.60) {
                                prices[prices.length - 1].click(); return true;
                            }
                        }
                    }
                }
                return false;
            }
        """)
        if not clicked:
            raise RuntimeError("No option Ask button found")
        page.wait_for_timeout(1500)
        _dismiss_options_overlays(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        account_trigger = page.get_by_text("Select an account to continue", exact=False).first
        if not account_trigger.is_visible(timeout=1200):
            account_trigger = page.get_by_text("Select account", exact=False).first
        account_trigger.click(timeout=3000)
        page.wait_for_timeout(1500)
        picker_text = page.locator("body").inner_text(timeout=5000)
        balances = []
        for match in re.finditer(
            r"(?:Non-registered|Unregistered|Personal)\s+"
            r"\$[0-9][0-9,.]*\s*CAD\s*[·•-]\s*\$([0-9][0-9,.]*)\s*USD",
            picker_text,
            re.IGNORECASE,
        ):
            balances.append(float(match.group(1).replace(",", "")))
        page.keyboard.press("Escape")
        if balances and max(balances) > 0:
            best = max(balances)
            print(f"  Found best Non-registered USD cash: ${best:.2f}")
            return best
    except Exception as exc:
        safe_print(f"  Options balance fallback failed: {exc}")

    return None


def is_login_page(page) -> bool:
    """Detect current and legacy Wealthsimple login surfaces."""
    try:
        if "/login" in page.url.lower():
            return True
        selectors = [
            'input[type="password"]', 'input[name="email"]',
            'button:has-text("Log in with a password")',
        ]
        return any(page.locator(selector).first.is_visible(timeout=500) for selector in selectors)
    except Exception:
        return False


def try_auto_login(page) -> bool:
    """
    Auto-login when session has expired back to the login page.

    Flow: click email box → ArrowDown to highlight the saved account suggestion
    → Enter to apply autofill (fills both email + password) → click Log In.

    Falls back to WS_EMAIL / WS_PASSWORD from .env if autofill leaves fields empty.
    NEVER submits if the email field is still empty after all attempts.
    """
    import os

    # Load .env so WS_EMAIL / WS_PASSWORD are available (Python doesn't auto-load it)
    env_file = ROOT / ".env"
    if env_file.exists():
        for _line in env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

    ws_email = os.environ.get("WS_EMAIL", "")
    ws_password = os.environ.get("WS_PASSWORD", "")

    if not ws_email or not ws_password:
        print("  Auto-login: WS_EMAIL / WS_PASSWORD not set in .env — cannot auto-login")
        return False

    print(f"  Session expired — auto-logging in as {ws_email[:12]}...")
    snap(page, "login_page")

    try:
        # Current macOS/web flow initially shows only passkey/password choices.
        if first_visible(page, [
            'input[type="email"]', 'input[name="email"]',
            'input[autocomplete*="email"]', 'input[autocomplete="username"]',
        ], timeout=1000) is None:
            try:
                page.get_by_text("Log in with a password", exact=True).click(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

        # Fill email
        email_input = first_visible(page, [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete*="email"]',
            'input[autocomplete="username"]',
            'input[placeholder*="email" i]',
        ], timeout=4000)
        if email_input is None:
            print("  Auto-login: email input not found")
            return False
        email_input.click()
        page.wait_for_timeout(300)
        email_input.fill(ws_email)
        page.wait_for_timeout(300)

        # Fill password
        pwd = first_visible(page, [
            'input[type="password"]',
            'input[placeholder*="Password" i]',
        ], timeout=3000)
        if pwd is None:
            print("  Auto-login: password input not found")
            return False
        pwd.click()
        page.wait_for_timeout(300)
        pwd.fill(ws_password)
        page.wait_for_timeout(400)

        snap(page, "before_login_click")

        # Click Log In
        login_clicked = False
        for label in ["Log in", "Login", "Sign in", "Sign In"]:
            try:
                btn = page.get_by_text(label, exact=True).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    login_clicked = True
                    break
            except Exception:
                pass
        if not login_clicked:
            try:
                page.locator('button[type="submit"]').first.click(timeout=2000)
                login_clicked = True
            except Exception:
                pass

        if not login_clicked:
            print("  Auto-login: Log In button not found")
            return False

        # Wait for redirect away from login page
        page.wait_for_timeout(8000)
        snap(page, "after_auto_login")

        still_on_login = is_login_page(page)

        if still_on_login:
            print("  Auto-login: still on login page — check credentials or 2FA required")
            return False

        print("  Auto-login: success!")
        return True

    except Exception as e:
        safe_print(f"  Auto-login error: {e}")
        return False


def cmd_balance(_args) -> None:
    from playwright.sync_api import sync_playwright

    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)

            if is_login_page(page):
                if not try_auto_login(page):
                    print("SESSION_EXPIRED: Wealthsimple session expired — run: python scripts/wealthsimple_auto.py setup")
                    sys.exit(1)
                page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)

            balance = get_live_balance(page)
            if balance is not None:
                print(f"LIVE_BALANCE_USD:{balance:.2f}")
                print(f"[OK] Live balance fetched: ${balance:.2f} USD")
            else:
                print("[ERROR] Could not find balance on page.")
                snap(page, "balance_fail")
                sys.exit(1)
    finally:
        _release_busy_lock()


def open_browser(p):
    # Connect to the already-running Chrome (launched by setup) via remote debugging.
    # This reuses the live logged-in session without ever closing/reopening the browser.
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, page
    except Exception:
        pass
    # Recover a crashed browser with the existing persistent profile.
    import subprocess
    import time
    browser_exe = find_browser_executable()
    safe_print("Browser unavailable — relaunching with the persistent Wealthsimple profile...")
    subprocess.Popen([
        browser_exe, "--remote-debugging-port=9222", f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check", WS_HOME,
    ])
    for _ in range(20):
        time.sleep(1)
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return ctx, page
        except Exception:
            continue
    print("No running browser found. Run: python scripts/wealthsimple_auto.py setup")
    sys.exit(1)


def save_session(ctx) -> None:
    try:
        ctx.storage_state(path=str(AUTH))  # backup copy as JSON
    except Exception:
        pass  # persistent context already saved to PROFILE_DIR


def navigate_to_stock(page, ws_symbol: str):
    print("Loading Wealthsimple home...")
    page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)
    snap(page, "home")

    if is_login_page(page):
        if not try_auto_login(page):
            raise RuntimeError("Wealthsimple session expired — run: python scripts/wealthsimple_auto.py setup")
        page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)

    # Wealthsimple's /app/trade/SYMBOL route currently redirects to /app/404.
    # Always use the site's own search UI and explicitly select the US result.
    print(f"Searching Wealthsimple for {ws_symbol}...")
    ctx = page.context
    search = first_visible(page, [
        'button[aria-label*="search" i]',
        '[role="button"][aria-label*="search" i]',
        '[data-testid*="search"]',
    ], timeout=5000)
    if search is None:
        snap(page, "search_not_found")
        raise RuntimeError("Wealthsimple search button not found")

    pages_before = set(id(p) for p in ctx.pages)
    search.click(force=True)
    page.wait_for_timeout(700)

    search_input = first_visible(page, [
        'input[placeholder*="Search" i]',
        'input[type="search"]',
        '[role="dialog"] input',
        'input:visible',
    ], timeout=5000)
    if search_input is None:
        snap(page, "search_input_not_found")
        raise RuntimeError("Wealthsimple search opened but its input did not appear")
    search_input.fill(ws_symbol)
    page.wait_for_timeout(2200)
    snap(page, "search_results")

    # The first exact ticker label is the primary result (e.g. SPY / NYSE).
    # Clicking the label bubbles to Wealthsimple's clickable result row.
    clicked = False
    try:
        ticker = page.get_by_text(ws_symbol.upper(), exact=True).first
        ticker.wait_for(state="visible", timeout=5000)
        ticker.click()
        clicked = True
    except Exception:
        pass
    if not clicked:
        snap(page, "search_result_not_found")
        raise RuntimeError(f"No Wealthsimple search result found for {ws_symbol}")

    page.wait_for_timeout(3000)

    new_pages = [p for p in ctx.pages if id(p) not in pages_before]
    if new_pages:
        page = new_pages[-1]
        page.bring_to_front()
        page.wait_for_timeout(2000)

    snap(page, "stock_page")
    return page


def choose_unregistered_account(page, side: str) -> None:
    print("Selecting Unregistered account...")
    opened = False
    selectors = [
        'button:has-text("Select")',
        '[role="button"]:has-text("Select")',
        '[data-testid*="account"]',
        'div:has-text("Select an account")',
        'div:has-text("Selectanaccount")',
    ]
    try:
        page.get_by_text("Select account", exact=False).first.click(timeout=2500)
        opened = True
    except Exception:
        opened = click_first(page, selectors, timeout=2500)

    if not opened:
        print("  Account picker not found - may already be pre-selected")
        return

    page.wait_for_timeout(900)
    snap(page, f"{side}_acct_picker")

    # Account names render before their balances. Wait for USD amounts so we
    # never guess between several identically named Non-registered accounts.
    try:
        page.wait_for_function(
            """() => /\\$[0-9][0-9,.]*\\s*USD/.test(document.body.innerText)""",
            timeout=10_000,
        )
    except Exception:
        snap(page, f"{side}_acct_balances_missing")
        raise RuntimeError(
            "Account balances did not load; refusing to guess between Non-registered accounts"
        )

    # Pick the Non-registered / Unregistered / Personal account with the highest USD balance.
    # "USD" alone must NOT be used as a search term — every account row contains the word "USD"
    # and it would match TFSA ($0.00 USD) before the real trading account.
    found = False
    best_label = None
    best_usd = 0.0
    labels = page.get_by_text(re.compile(r"^(Non-registered|Unregistered|Personal)$", re.I)).all()
    for label in labels:
        try:
            node = label
            row_text = ""
            for _ in range(6):
                node = node.locator("xpath=..")
                row_text = node.inner_text(timeout=500)
                if re.search(r"\$[0-9][0-9,.]*\s*USD", row_text, re.I):
                    break
            match = re.search(r"\$([0-9][0-9,.]*)\s*USD", row_text, re.I)
            usd = float(match.group(1).replace(",", "")) if match else 0.0
            if usd > best_usd:
                best_usd = usd
                best_label = label
        except Exception:
            continue

    if best_label is not None and best_usd > 0:
        best_label.click(force=True)
        found = True

    if found:
        page.wait_for_timeout(700)
        snap(page, f"{side}_acct_selected")
        print(f"  Selected Non-registered account with ${best_usd:.2f} USD")
        return

    snap(page, f"{side}_acct_fail")
    raise RuntimeError(
        f"Non-registered account with positive USD cash not found - "
        f"see data/screen_{side}_acct_fail.png"
    )


def place_order(
    page,
    side: str,
    shares: Optional[float],
    price: Optional[float],
    confirm: bool,
    max_dollars: bool = False,
    sell_all: bool = False,
) -> dict:
    from playwright.sync_api import TimeoutError as PWTimeout

    print(f"Clicking {side.title()} tab...")
    tab_label = side.title()  # "Buy" or "Sell"
    tab_clicked = page.evaluate(f"""
        () => {{
            const tabs = [...document.querySelectorAll('button, [role="tab"]')];
            const tab = tabs.find(el => el.textContent.trim() === '{tab_label}');
            if (tab) {{ tab.click(); return true; }}
            return false;
        }}
    """)
    if not tab_clicked:
        snap(page, f"{side}_tab_fail")
        raise RuntimeError(f"Could not find {side.title()} tab")
    page.wait_for_timeout(800)
    snap(page, f"{side}_tab")

    if price is not None:
        print(f"Setting Limit order type ({side})...")
        # Use is_visible() so hidden dropdown options don't trigger a false positive
        try:
            market_btn_visible = page.locator('button:has-text("Market")').first.is_visible(timeout=500)
        except Exception:
            market_btn_visible = False

        if market_btn_visible:
            # Currently in Market mode — switch to Limit
            try:
                page.locator('button:has-text("Market")').first.click(timeout=3000)
                page.wait_for_timeout(400)
                page.locator(
                    'li:has-text("Limit"), button:has-text("Limit"), [role="option"]:has-text("Limit")'
                ).first.click(timeout=3000)
                page.wait_for_timeout(600)
            except PWTimeout:
                print("  Warning: could not switch to Limit - will still try to fill price")
        else:
            print("  Already in Limit mode (extended hours) — skipping type switch")

        snap(page, "limit_selected")

        print(f"Entering limit price ${price:.2f}...")
        try:
            # Try label-targeted input first; fall back to index 0
            limit_input = page.get_by_label("Limit price", exact=False).first
            if limit_input.is_visible(timeout=1000):
                limit_input.click()
                page.keyboard.press(SELECT_ALL)
                page.keyboard.type(f"{price:.2f}", delay=50)
            else:
                fill_visible_input(page, 0, f"{price:.2f}")
            page.wait_for_timeout(400)
        except Exception as e:
            print(f"  Warning: could not fill limit price: {e}")
            price = None
    else:
        print("Keeping Market order type.")

    choose_unregistered_account(page, side)

    # For buys, prioritize using the "Max" button to let WS calculate whole shares
    if side == "buy":
        print("Using Dollars -> Max for optimal whole-share calculation...")
        use_max_dollars(page)
        snap(page, f"{side}_max")
    elif sell_all and side == "sell":
        use_max_shares(page)
        # In limit-sell mode WS Max button may give only the whole-share count.
        # Re-enter the exact fractional quantity we were given so nothing is left behind.
        if price is not None and shares is not None and shares > 0:
            try:
                page.wait_for_timeout(300)
                # Format as decimal without trailing zeros (e.g. "1.5941" not "1.59410000")
                qty_str = f"{shares:.6f}".rstrip("0").rstrip(".")
                fill_visible_input(page, 1, qty_str)
                page.wait_for_timeout(300)
            except Exception:
                pass  # keep whatever Max populated
        snap(page, f"{side}_max")
    else:
        if shares is None:
            raise RuntimeError("Shares are required unless --max-dollars or --sell-all is used.")
        print(f"Entering {shares} shares...")
        try:
            if price is None:
                fill_order_quantity(page, shares)
            else:
                fill_visible_input(page, 1, str(shares))
        except Exception:
            snap(page, f"{side}_qty_fail")
            raise RuntimeError(f"Shares input not found - see data/screen_{side}_qty_fail.png")
        page.wait_for_timeout(500)
        snap(page, f"{side}_qty")

    print("Clicking Next...")
    next_clicked = page.evaluate("""
        () => {
            const btn = [...document.querySelectorAll('button')].find(el => el.textContent.trim() === 'Next');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """)
    if not next_clicked:
        snap(page, f"{side}_next_fail")
        raise RuntimeError("Next button not found")
    page.wait_for_timeout(3000)
    snap(page, f"{side}_review")
    # Capture review page text NOW — before submit changes the page
    _review_text = ""
    try:
        _review_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        pass

    funding_match = re.search(r"You need\s+\$?([0-9,.]+)\s+USD more", _review_text, re.IGNORECASE)
    if funding_match:
        needed = funding_match.group(1)
        raise RuntimeError(
            f"Insufficient USD cash in selected account (Wealthsimple needs ${needed} USD more). "
            "Convert/add USD manually before running live automation."
        )
    submitted = False

    if confirm:
        print("Placing order (confirm)...")
        # Extended-hours limit orders show an intermediate "Review" step before the final submit
        try:
            review_intermediate = page.locator('button:has-text("Review")').first
            if review_intermediate.is_visible(timeout=1500):
                print("  Intermediate Review step — clicking through...")
                review_intermediate.click()
                page.wait_for_timeout(2000)
                snap(page, f"{side}_review2")
                try:
                    _review_text = page.locator("body").inner_text(timeout=3000)
                except Exception:
                    pass
        except Exception:
            pass
        # Use JS click to bypass chat-widget overlay that intercepts pointer events
        submitted_via_js = page.evaluate("""
            () => {
                const texts = [
                    'Submit order', 'Submit Order',
                    'Place order', 'Place Order',
                    'Queue order', 'Queue Order',
                    'Confirm order', 'Confirm Order',
                    'Submit buy order', 'Submit Buy Order',
                    'Submit sell order', 'Submit Sell Order',
                    'Submit limit order', 'Submit Limit Order',
                ];
                const buttons = [...document.querySelectorAll('button, [role="button"], [role="submit"]')];
                for (const text of texts) {
                    const btn = buttons.find(b =>
                        b.textContent.trim() === text ||
                        b.textContent.trim().toLowerCase() === text.toLowerCase()
                    );
                    if (btn) { btn.click(); return 'clicked:' + text; }
                }
                const submitBtn = document.querySelector('button[type="submit"]');
                if (submitBtn) { submitBtn.click(); return 'clicked:type=submit'; }
                const allText = document.body.innerText.substring(0, 2000);
                return 'not_found::' + allText;
            }
        """)
        if submitted_via_js and submitted_via_js.startswith("clicked:"):
            safe_print(f"  Submit clicked via: {submitted_via_js}")
        else:
            debug_text = (submitted_via_js or "").replace("not_found::", "")[:1000] if submitted_via_js else "no text"
            safe_print(f"  Page text at submit: {debug_text[:500]}")
            snap(page, f"{side}_confirm_fail")
            raise RuntimeError(f"Submit button not found - see data/screen_{side}_confirm_fail.png")
        # Mark submitted BEFORE any post-submit page ops — the confirmation page
        # can navigate/crash and we must not lose the fact the order went through.
        submitted = True
        print("  Order submitted.")
        try:
            page.wait_for_timeout(3000)
            snap(page, f"{side}_done")
        except Exception:
            pass
    else:
        print("  Stopped at review page - confirm manually.")

    try:
        return parse_review_details(page, side, submitted, review_text=_review_text)
    except Exception:
        return {"side": side, "submitted": submitted}


def cmd_setup(_args) -> None:
    import subprocess

    PROFILE_DIR.mkdir(exist_ok=True)

    # Kill any leftover Edge/Chrome using our profile so we can launch fresh
    import psutil
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "msedge" in name or "chrome" in name:
                cmdline = " ".join(proc.info["cmdline"] or [])
                if "browser_profile" in cmdline or "9222" in cmdline:
                    proc.kill()
        except Exception:
            pass

    browser_exe = find_browser_executable()
    subprocess.Popen([
        browser_exe,
        "--remote-debugging-port=9222",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        WS_HOME,
    ])

    print()
    print("=" * 55)
    print("  WEALTHSIMPLE LOGIN")
    print("=" * 55)
    print(f"  1. A browser window just opened ({Path(browser_exe).name}).")
    print("  2. Log in to Wealthsimple normally.")
    print("  3. Wait until you can see your HOME page / portfolio.")
    print("  4. Come back here and press ENTER.")
    print("=" * 55)
    try:
        input("  >> Press ENTER when you are on the home page: ")
    except EOFError:
        print("  (non-interactive mode — waiting...)")
        import time
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{CDP_URL}/json", timeout=2)
                break
            except Exception:
                time.sleep(2)
    print("  Done. The browser will stay open — the bot connects to it for all operations.")
    print("  Keep that browser window running in the background.")


def cmd_position(args) -> None:
    """Read the actual open position details (fill_price, shares) from Wealthsimple for a given symbol."""
    from playwright.sync_api import sync_playwright

    symbol = strip_exchange(args.symbol.upper())
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)

            if is_login_page(page):
                if not try_auto_login(page):
                    print("SESSION_EXPIRED: Wealthsimple session expired")
                    sys.exit(1)
                page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)

            fill_data = read_position_from_trade_page(page, symbol)
            if fill_data:
                print("ORDER_RESULT_JSON:" + json.dumps(fill_data, sort_keys=True))
            else:
                print("POSITION_NOT_FOUND")
    finally:
        _release_busy_lock()


def cmd_quote(args) -> None:
    """Print live Wealthsimple price for a symbol (covers overnight/ATS sessions)."""
    symbol = strip_exchange(args.symbol.upper())
    shares = float(args.shares) if args.shares else None
    price  = get_ws_price(symbol, shares=shares)
    print(f"WS_PRICE:{price:.4f}" if price else "WS_PRICE:0")


def cmd_buy(args) -> None:
    from playwright.sync_api import sync_playwright

    result: dict = {"side": "buy", "submitted": False, "symbol": args.symbol}
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            try:
                page = navigate_to_stock(page, strip_exchange(args.symbol))
                result = place_order(
                    page,
                    "buy",
                    args.shares,
                    args.price,
                    confirm=True,
                    max_dollars=args.max_dollars,
                )
                result["symbol"] = args.symbol
                label = "Dollars Max (Market)" if args.max_dollars else f"{args.shares} shares (Market)"
                if args.price:
                    label = f"{args.shares} shares @ ${args.price:.2f}"
                safe_print(f"\n[OK] Buy order: {args.symbol} {label}")

                if result.get("submitted"):
                    fill_data = read_position_from_trade_page(page, strip_exchange(args.symbol))
                    if fill_data:
                        result.update(fill_data)
                        safe_print(f"  [fill] Actual position: {fill_data['fill_quantity']:.4f} sh @ ${fill_data['fill_price']:.4f}")
                    else:
                        safe_print("  [fill] Position not yet visible on trade page — will use estimate")
            except Exception as e:
                safe_print(f"\n[ERROR] Buy failed: {e}")
            finally:
                print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
    finally:
        _release_busy_lock()
    if not result.get("submitted"):
        sys.exit(1)


def cmd_sell(args) -> None:
    from playwright.sync_api import sync_playwright

    result: dict = {"side": "sell", "submitted": False, "symbol": args.symbol}
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            try:
                page = navigate_to_stock(page, strip_exchange(args.symbol))
                n_cancelled = cancel_pending_on_stock_page(page)
                if n_cancelled:
                    print(f"  Cleared {n_cancelled} pending order(s) before sell")
                    page.wait_for_timeout(800)
                result = place_order(page, "sell", args.shares, args.price, confirm=True, sell_all=args.sell_all)
                result["symbol"] = args.symbol
                label = "all shares" if args.sell_all else f"{args.shares} shares"
                order_type = f"Limit @ ${args.price:.2f}" if args.price else "Market"
                safe_print(f"\n[OK] Sell order: {label} x {args.symbol} ({order_type})")
            except Exception as e:
                safe_print(f"\n[ERROR] Sell failed: {e}")
            finally:
                print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
    finally:
        _release_busy_lock()
    if not result.get("submitted"):
        sys.exit(1)


def navigate_to_spy_options(page):
    """
    Navigate to Wealthsimple's SPY options chain and dismiss any modal dialogs.
    WS shows an 'Options' tab on the SPY stock page that loads the chain inline.
    """
    print("Loading SPY stock page...")
    page = navigate_to_stock(page, "SPY")
    page.wait_for_timeout(2000)
    snap(page, "spy_stock_page")

    # The React page occasionally renders Stocks before mounting the Options
    # tab. Retry with a full reload instead of failing the day's trade.
    clicked_options = False
    for attempt in range(1, 4):
        for sel in [
            'button:has-text("Options")',
            '[role="tab"]:has-text("Options")',
            'a:has-text("Options")',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click()
                    page.wait_for_timeout(2500)
                    snap(page, "options_tab")
                    print(f"  Options tab clicked via: {sel}")
                    clicked_options = True
                    break
            except Exception:
                continue
        if clicked_options:
            break
        print(f"  Options tab missing (attempt {attempt}/3) — navigating to SPY again...")
        page = navigate_to_stock(page, "SPY")
        page.wait_for_timeout(2000)

    if not clicked_options:
        snap(page, "options_tab_not_found")
        raise RuntimeError(
            "Options tab not found on SPY page — "
            "check data/screen_spy_stock_page.png"
        )

    # Kill all popups/panels — call twice because WS sometimes re-opens the AI panel
    _dismiss_options_overlays(page)
    page.wait_for_timeout(500)
    _dismiss_options_overlays(page)

    snap(page, "options_chain_ready")
    return page


def _dismiss_options_overlays(page) -> None:
    """
    Close all WS options-page overlays on every navigation:
      - 'What's your view on SPY?' panel  → aria-label="Close chat panel"
      - 'Build a trade with AI' modal      → "Maybe later" button
      - Any other close/× button on screen
    """
    # 1. 'What's your view on SPY?' panel. The close button sometimes has no
    # accessible label, so locate the panel from its heading and click the
    # top-right button inside that same container.
    closed_view_panel = page.evaluate("""
        () => {
            const heading = [...document.querySelectorAll('*')].find(el =>
                (el.textContent || '').trim() === "What's your view on SPY?"
            );
            if (!heading) return false;
            let panel = heading.parentElement;
            for (let i = 0; i < 8 && panel; i++, panel = panel.parentElement) {
                const rect = panel.getBoundingClientRect();
                if (rect.width < 300 || rect.height < 300) continue;
                const buttons = [...panel.querySelectorAll('button, [role="button"]')]
                    .filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.x > rect.x + rect.width * 0.7
                            && r.y < rect.y + rect.height * 0.2;
                    });
                if (buttons.length) { buttons[0].click(); return true; }
            }
            return false;
        }
    """)
    if closed_view_panel:
        print("  Closed 'What's your view on SPY?' panel via heading container")
        page.wait_for_timeout(500)

    # Accessible-label fallback.
    for sel in [
        '[aria-label="Close chat panel"]',
        '[aria-label="close chat panel"]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=800):
                btn.click()
                page.wait_for_timeout(500)
                print(f"  Closed 'What's your view' panel via: {sel}")
        except Exception:
            continue

    # 2. 'Build a trade with AI' modal + 'Add money' / 'Fund your account' modals
    for dismiss_text in ["Maybe later", "Maybe Later", "No thanks", "Not now", "Skip", "Dismiss", "Close"]:
        try:
            btn = page.get_by_text(dismiss_text, exact=True).first
            if btn.is_visible(timeout=600):
                btn.click()
                page.wait_for_timeout(500)
                print(f"  Dismissed modal via: '{dismiss_text}'")
                break
        except Exception:
            continue

    # 3. 'Add money' deposit modal — dismiss via its own close button
    page.evaluate("""
        () => {
            const addMoneyTexts = ['add money', 'make a deposit', 'fund your account', 'deposit funds'];
            const modals = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [class*="modal" i], [class*="Modal" i]')];
            for (const modal of modals) {
                const txt = (modal.textContent || '').toLowerCase();
                if (!addMoneyTexts.some(t => txt.includes(t))) continue;
                // Find and click the close/dismiss button inside the modal
                const closeBtn = modal.querySelector(
                    '[aria-label*="close" i], [aria-label*="dismiss" i], button[class*="close" i]'
                );
                if (closeBtn) { closeBtn.click(); return; }
                // Fall back to any × or Cancel button
                const btns = [...modal.querySelectorAll('button')];
                const skipBtn = btns.find(b => /maybe later|not now|skip|cancel|close|dismiss/i.test(b.textContent));
                if (skipBtn) { skipBtn.click(); }
            }
        }
    """)
    page.wait_for_timeout(300)

    # 4. Any remaining × / Close buttons in the upper-right area of the screen
    page.evaluate("""
        () => {
            const closeSymbols = new Set(['×', '✕', '✖', '⨯']);
            const all = [...document.querySelectorAll('button, [role="button"]')];
            for (const el of all) {
                const txt   = (el.textContent || el.innerText || '').trim();
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const rect  = el.getBoundingClientRect();
                if (rect.width === 0) continue;
                if ((closeSymbols.has(txt) || label.includes('close'))
                        && rect.x > window.innerWidth * 0.4
                        && rect.y < 250) {
                    el.click();
                }
            }
        }
    """)
    page.wait_for_timeout(300)


def select_option_expiry(page, expiry: str) -> bool:
    """
    Select today's expiry on the WS options chain.
    WS defaults to the nearest expiry (today for 0DTE) so this is usually a no-op.
    Only attempts to click a date tab if multiple expiries are visible.
    """
    from datetime import datetime as _dt
    exp_dt = _dt.strptime(expiry, "%Y-%m-%d")
    # Build labels without Linux-only %-d (use str(int()) to strip leading zeros)
    month_short = exp_dt.strftime("%b")   # "Jun"
    month_long  = exp_dt.strftime("%B")   # "June"
    day         = str(exp_dt.day)         # "9"  (no leading zero)
    day_padded  = exp_dt.strftime("%d")   # "09"
    month_num   = str(exp_dt.month)       # "6"
    month_padded= exp_dt.strftime("%m")   # "06"
    year        = str(exp_dt.year)

    labels = [
        f"{month_num}/{day}",                  # 6/9
        f"{month_padded}/{day_padded}",         # 06/09
        f"{month_short} {day}",                # Jun 9
        f"{month_short} {day_padded}",         # Jun 09
        f"{month_long} {day}, {year}",         # June 9, 2026
        expiry,                                # 2026-06-09
    ]
    for label in labels:
        try:
            el = page.locator(f':text("{label}")').first
            if el.is_visible(timeout=800):
                el.click()
                page.wait_for_timeout(1000)
                snap(page, "options_expiry_selected")
                print(f"  Selected expiry: {label}")
                return True
        except Exception:
            continue

    print(f"  Expiry selector not needed — WS defaults to today ({expiry})")
    return True


def find_and_click_option_contract(
    page,
    option_type: str,
    strike: float,
) -> bool:
    """
    Select the correct Call/Put side, then click the green Ask(Buy) button
    in the row matching the target strike.

    WS options chain UI (confirmed from screenshot):
      - "Call" / "Put" toggle buttons at top-left of chain
      - Each row: Strike column | ... | Ask(Buy) green button
      - The green Ask(Buy) button opens the order ticket
    """
    strike_label = f"${int(strike)}"
    snap(page, "options_chain_before_select")

    # Step 1: click Call or Put toggle to show the right side
    print(f"  Selecting {option_type.upper()} side...")
    type_label = "Call" if option_type == "call" else "Put"
    try:
        toggle = page.get_by_text(type_label, exact=True).first
        toggle.wait_for(state="visible", timeout=3000)
        toggle.click()
        page.wait_for_timeout(1000)
        print(f"  Clicked '{type_label}' toggle")
    except Exception as e:
        print(f"  [WARN] Could not click '{type_label}' toggle: {e}")

    snap(page, f"options_{option_type}_selected")

    # Step 2: find the row with this strike and click the Ask(Buy) button.
    # Chain columns (left→right): Strike | Breakeven | %toBE | OI | Volume | Mid | Bid(Sell) | Ask(Buy)
    # Ask(Buy) is always the LAST (rightmost) price button in each row — green coloured.
    # Bid(Sell) is second-to-last — red/pink. We must click the last one.
    print(f"  Looking for strike {strike_label} Ask(Buy) button...")

    def _click_ask_js(label: str) -> str:
        return page.evaluate(f"""
            () => {{
                const strikeLabel = '{label}';

                // Find the leaf element whose exact text matches the strike label
                const strikeEl = [...document.querySelectorAll('*')].find(el => {{
                    const txt = (el.textContent || el.innerText || '').trim();
                    return txt === strikeLabel && el.children.length === 0;
                }});
                if (!strikeEl) return 'strike_not_found';

                // Walk up until we find a container that has >= 2 price buttons
                let row = strikeEl.parentElement;
                for (let i = 0; i < 10; i++) {{
                    if (!row) return 'no_row';
                    const priceBtns = [...row.querySelectorAll('button')].filter(
                        b => /^\\$[0-9]/.test((b.textContent || '').trim())
                    );
                    if (priceBtns.length >= 2) {{
                        // Ask(Buy) is the LAST price button (rightmost column)
                        const askBtn = priceBtns[priceBtns.length - 1];
                        askBtn.click();
                        return 'clicked_ask:' + askBtn.textContent.trim();
                    }}
                    row = row.parentElement;
                }}
                return 'row_not_found';
            }}
        """)

    result = _click_ask_js(strike_label)
    print(f"  JS result: {result}")

    if result and result.startswith("clicked_ask"):
        page.wait_for_timeout(2000)
        snap(page, "options_ask_clicked")
        return True

    # Fallback: scroll the strike into view, then retry
    try:
        page.locator(f'text="{strike_label}"').first.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(600)
        result2 = _click_ask_js(strike_label)
        print(f"  JS result after scroll: {result2}")
        if result2 and result2.startswith("clicked_ask"):
            page.wait_for_timeout(2000)
            snap(page, "options_ask_clicked_v2")
            return True
    except Exception as e:
        print(f"  Scroll fallback failed: {e}")

    snap(page, "options_contract_not_found")
    return False


def read_option_chain_quote(page, option_type: str, strike: float, expiry: str) -> dict:
    """Read Wealthsimple's displayed bid/ask for one exact contract without ordering."""
    page = navigate_to_spy_options(page)
    select_option_expiry(page, expiry)
    type_label = "Call" if option_type.lower() == "call" else "Put"
    page.get_by_text(type_label, exact=True).first.click(timeout=3000)
    page.wait_for_timeout(900)
    label = f"${int(strike)}"
    quote = page.evaluate(r"""(strikeLabel) => {
        const strike = [...document.querySelectorAll('*')].find(el =>
            el.children.length === 0 && (el.textContent || '').trim() === strikeLabel
        );
        if (!strike) return null;
        let row = strike.parentElement;
        for (let i = 0; i < 10 && row; i++, row = row.parentElement) {
            const prices = [...row.querySelectorAll('button')].filter(b =>
                /^\$[0-9]/.test((b.textContent || '').trim())
            );
            if (prices.length >= 2) {
                const value = b => parseFloat((b.textContent || '').replace('$', ''));
                return {bid: value(prices[0]), ask: value(prices[prices.length - 1])};
            }
        }
        return null;
    }""", label)
    if not quote:
        raise RuntimeError(f"Broker quote not found for {label} {option_type.upper()} {expiry}")
    quote["mid"] = round((quote["bid"] + quote["ask"]) / 2, 4)
    return quote


def cmd_option_quote(args) -> None:
    from playwright.sync_api import sync_playwright
    result = {}
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            _, page = open_browser(p)
            result = read_option_chain_quote(page, args.option_type, args.strike, args.expiry)
            print("OPTION_QUOTE_JSON:" + json.dumps(result, sort_keys=True))
    except Exception as exc:
        safe_print(f"[ERROR] option quote failed: {exc}")
    finally:
        _release_busy_lock()
    if not result:
        sys.exit(1)


def cmd_option_position(args) -> None:
    """Best-effort broker reconciliation from Holdings/Activity text."""
    from playwright.sync_api import sync_playwright
    result = {}
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            _, page = open_browser(p)
            page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(4000)
            text = page.locator("body").inner_text(timeout=5000)
            marker = rf"(?:SPY\s+)?(?:{re.escape(args.expiry)}|[A-Z][a-z]{{2}}\s+\d{{1,2}}).*?\${args.strike}.*?{args.option_type}"
            if not re.search(marker, text, re.IGNORECASE | re.DOTALL):
                # Activity often exposes fills even before Holdings updates.
                activity = first_visible(page, ['a[href*="activity"]', '[aria-label*="Activity" i]'], timeout=2000)
                if activity:
                    activity.click()
                    page.wait_for_timeout(3000)
                    text = page.locator("body").inner_text(timeout=5000)
            contract_match = re.search(marker, text, re.IGNORECASE | re.DOTALL)
            if not contract_match:
                raise RuntimeError("Exact contract is not visible in Holdings or Activity")
            start = max(contract_match.start() - 300, 0)
            text = text[start:contract_match.end() + 700]
            qty = None
            premium = None
            for pat in [r"([0-9]+)\s+contracts?", r"Contracts?\s+([0-9]+)"]:
                m = re.search(pat, text, re.IGNORECASE)
                if m: qty = int(m.group(1)); break
            for pat in [r"(?:Average|Avg\.?|Filled at|Fill price)\s*(?:price|cost)?\s*\$([0-9,.]+)", r"Price\s*\$([0-9,.]+)"]:
                m = re.search(pat, text, re.IGNORECASE)
                if m: premium = float(m.group(1).replace(",", "")); break
            if qty and premium:
                result = {"contracts": qty, "fill_price": premium, "fill_value": qty * premium * 100}
                print("OPTION_POSITION_JSON:" + json.dumps(result, sort_keys=True))
    except Exception as exc:
        safe_print(f"[ERROR] option position reconciliation failed: {exc}")
    finally:
        _release_busy_lock()
    if not result:
        sys.exit(1)


def cmd_cancel_option(args) -> None:
    """Cancel a pending SPY option order from the SPY options surface."""
    from playwright.sync_api import sync_playwright
    cancelled = 0
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            _, page = open_browser(p)
            page = navigate_to_spy_options(page)
            select_option_expiry(page, args.expiry)
            cancelled = cancel_pending_on_stock_page(page)
            print(f"OPTION_CANCELLED:{cancelled}")
    except Exception as exc:
        safe_print(f"[ERROR] pending option cancellation failed: {exc}")
    finally:
        _release_busy_lock()
    if cancelled < 1:
        sys.exit(1)


def place_option_order(page, side: str, n_contracts: int, confirm: bool, max_cost: float | None = None) -> dict:
    """
    Interact with the WS options order ticket (slides up as a drawer from the bottom).
    Handles the one-time 'Writing options' consent screen if present.
    """
    result: dict = {"side": side, "submitted": False}

    # Kill popups before interacting with the ticket
    _dismiss_options_overlays(page)

    # The ticket appears as a bottom drawer — scroll to the bottom to reach it
    print("Scrolling to options ticket drawer...")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1500)
    snap(page, f"option_{side}_ticket")

    # Handle one-time 'Writing options' / options-enablement consent screen
    for consent_text in ["Continue", "I understand", "Enable options", "Agree"]:
        try:
            btn = page.get_by_text(consent_text, exact=True).first
            if btn.is_visible(timeout=1000):
                btn.click()
                page.wait_for_timeout(1500)
                print(f"  Dismissed options consent via: '{consent_text}'")
                snap(page, f"option_{side}_consent_done")
                break
        except Exception:
            continue

    # Live option entries and exits use market orders during regular hours.
    # Weekend/closed-market "Queue order" remains intentionally excluded.
    if side in {"buy", "sell"}:
        market_label = "Market buy" if side == "buy" else "Market sell"
        switched = page.evaluate("""
            (marketLabel) => {
                const selects = [...document.querySelectorAll('select')];
                for (const sel of selects) {
                    const opts = [...sel.options].map(o => o.text.toLowerCase());
                    if (opts.some(o => o.includes('market') || o.includes('limit'))) {
                        const mktOpt = [...sel.options].find(o =>
                            o.text.toLowerCase().includes('market'));
                        if (mktOpt) { sel.value = mktOpt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'select:' + mktOpt.text; }
                    }
                }
                // Fallback: click the order type dropdown text and pick Market
                const els = [...document.querySelectorAll('button, [role="option"], li')];
                const mktBtn = els.find(el =>
                    (el.textContent || '').trim().toLowerCase() === marketLabel.toLowerCase());
                if (mktBtn) { mktBtn.click(); return 'click:' + mktBtn.textContent.trim(); }
                return 'not_found';
            }
        """, market_label)
        print(f"  Order type -> {market_label}: {switched}")
        if switched == "not_found":
            try:
                current_label = "Limit buy" if side == "buy" else "Limit sell"
                trigger = page.get_by_text(current_label, exact=True).first
                if not trigger.is_visible(timeout=1000):
                    trigger = page.locator('[data-testid*="order-type"], select').first
                trigger.click(timeout=2000)
                page.wait_for_timeout(400)
                page.get_by_text(market_label, exact=True).last.click(timeout=2000)
                switched = f"click:{market_label}"
                print(f"  Switched to {market_label} via Playwright click")
            except Exception as e:
                raise RuntimeError(f"Could not switch option order to {market_label}: {e}")
        page.wait_for_timeout(500)

    # Choose non-registered account
    choose_unregistered_account(page, f"option_{side}")

    # Enter number of contracts
    print(f"Entering {n_contracts} contract(s)...")
    try:
        inp = page.get_by_label("Contracts", exact=False).first
        if inp.is_visible(timeout=2000):
            inp.click()
            page.keyboard.press(SELECT_ALL)
            page.keyboard.type(str(n_contracts), delay=50)
        else:
            fill_visible_input(page, 0, str(n_contracts))
        page.wait_for_timeout(500)
    except Exception as e:
        print(f"  [WARN] Could not fill contracts input: {e}")
    snap(page, f"option_{side}_qty")

    # Click Next
    next_clicked = page.evaluate("""
        () => {
            const btn = [...document.querySelectorAll('button')].find(
                el => el.textContent.trim() === 'Next'
            );
            if (btn) { btn.click(); return true; }
            return false;
        }
    """)
    if not next_clicked:
        snap(page, f"option_{side}_next_fail")
        raise RuntimeError(
            f"Options ticket Next button not found — "
            f"see data/screen_option_{side}_next_fail.png"
        )
    page.wait_for_timeout(3000)

    # The SPY AI panel can reopen when Next is clicked. Close only that panel's
    # own top-right cross before reading the actual order review.
    page.evaluate("""
        () => {
            const heading = [...document.querySelectorAll('*')].find(el =>
                (el.textContent || '').trim() === "What's your view on SPY?"
            );
            if (!heading) return false;
            let panel = heading.parentElement;
            for (let i = 0; i < 8 && panel; i++, panel = panel.parentElement) {
                const rect = panel.getBoundingClientRect();
                if (rect.width < 300 || rect.height < 300) continue;
                const close = [...panel.querySelectorAll('button, [role="button"]')].find(b => {
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.x > rect.x + rect.width * 0.7
                        && r.y < rect.y + rect.height * 0.2;
                });
                if (close) { close.click(); return true; }
            }
            return false;
        }
    """)
    page.wait_for_timeout(700)
    snap(page, f"option_{side}_review")

    _review_text = ""
    try:
        _review_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        pass

    review = parse_review_details(page, side, False, review_text=_review_text)
    if side == "buy" and max_cost is not None:
        estimated = review.get("estimated_value")
        if estimated is None or estimated <= 0:
            raise RuntimeError("Could not verify option order cost on review page — refusing submission")
        if estimated > max_cost:
            raise RuntimeError(
                f"Option order estimated cost ${estimated:.2f} exceeds safety cap ${max_cost:.2f}"
            )

    if confirm:
        _dismiss_options_overlays(page)  # panel often reopens on the review page
        submitted_via_js = page.evaluate("""
            () => {
                // NOTE: 'Queue order' is intentionally excluded — that means
                // the market is closed and the order would be queued pre-market.
                // The bot only enters between 9:45 AM and 10:00 AM ET so this
                // should never appear during normal operation.
                const texts = [
                    'Submit order', 'Submit Order',
                    'Place order', 'Place Order',
                    'Confirm order', 'Confirm Order',
                    'Submit buy order', 'Submit Buy Order',
                    'Submit sell order', 'Submit Sell Order',
                    'Sell order', 'Sell Order',
                    'Place sell order', 'Place Sell Order',
                    'Sell', 'Confirm sell', 'Confirm Sell',
                ];
                const buttons = [...document.querySelectorAll('button, [role="button"]')];
                for (const text of texts) {
                    const btn = buttons.find(b =>
                        b.textContent.trim() === text ||
                        b.textContent.trim().toLowerCase() === text.toLowerCase()
                    );
                    if (btn) { btn.click(); return 'clicked:' + text; }
                }
                const submitBtn = document.querySelector('button[type="submit"]');
                if (submitBtn) { submitBtn.click(); return 'clicked:type=submit'; }
                return 'not_found';
            }
        """)
        if submitted_via_js and submitted_via_js.startswith("clicked:"):
            safe_print(f"  Submit clicked via: {submitted_via_js}")
            result["submitted"] = True
            page.wait_for_timeout(3000)
            snap(page, f"option_{side}_done")
        else:
            snap(page, f"option_{side}_submit_fail")
            raise RuntimeError(
                f"Options submit button not found — "
                f"see data/screen_option_{side}_submit_fail.png"
            )
    else:
        print("  Stopped at review — pass --confirm to submit")

    result.update(parse_review_details(page, side, result["submitted"], review_text=_review_text))
    return result


def cmd_buy_option(args) -> None:
    from playwright.sync_api import sync_playwright

    result: dict = {
        "side": "buy", "submitted": False,
        "symbol": args.symbol, "option_type": args.option_type,
        "strike": args.strike, "expiry": args.expiry,
    }
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            try:
                page = navigate_to_spy_options(page)
                select_option_expiry(page, args.expiry)
                found = find_and_click_option_contract(page, args.option_type, float(args.strike))
                if not found:
                    raise RuntimeError(
                        f"Contract not found: {args.option_type.upper()} ${args.strike} "
                        f"exp {args.expiry} — see screenshots in data/"
                    )
                result = place_option_order(page, "buy", args.contracts, confirm=args.confirm, max_cost=args.max_cost)
                result.update({
                    "symbol": args.symbol, "option_type": args.option_type,
                    "strike": args.strike, "expiry": args.expiry,
                })
                safe_print(
                    f"\n[OK] Buy option: {args.contracts}x SPY ${args.strike} "
                    f"{args.option_type.upper()} {args.expiry}"
                )
            except Exception as e:
                safe_print(f"\n[ERROR] buy-option failed: {e}")
            finally:
                print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
    finally:
        _release_busy_lock()
    if not result.get("submitted") and args.confirm:
        sys.exit(1)


def sell_option_from_portfolio(
    page,
    option_type: str,
    strike: float,
    n_contracts: int,
    confirm: bool,
    expiry: str = "",
) -> dict:
    """
    Sell/close an existing options position via the options chain Bid(Sell) button.

    Mirrors the buy flow but clicks the Bid (first price button) instead of
    the Ask (last price button). This reliably opens the 'sell to close' ticket.
    """
    from datetime import date as _date
    strike_int = int(strike)
    type_lower = option_type.lower()
    expiry_str = expiry or _date.today().isoformat()

    print(f"Navigating to SPY options chain to close {option_type.upper()} ${strike_int} position...")
    page = navigate_to_spy_options(page)
    snap(page, "sell_option_portfolio_page")

    select_option_expiry(page, expiry_str)

    # Select call/put side
    type_label = "Call" if type_lower == "call" else "Put"
    try:
        toggle = page.get_by_text(type_label, exact=True).first
        toggle.wait_for(state="visible", timeout=3000)
        toggle.click()
        page.wait_for_timeout(1000)
        print(f"  Clicked '{type_label}' toggle")
    except Exception as e:
        print(f"  [WARN] Could not click '{type_label}' toggle: {e}")

    snap(page, "sell_option_chain_before_close")

    strike_label = f"${strike_int}"
    print(f"  Looking for strike {strike_label} Bid(Sell) button...")

    def _click_bid_js(label: str) -> str:
        return page.evaluate(f"""
            () => {{
                const strikeLabel = '{label}';
                const strikeEl = [...document.querySelectorAll('*')].find(el => {{
                    const txt = (el.textContent || el.innerText || '').trim();
                    return txt === strikeLabel && el.children.length === 0;
                }});
                if (!strikeEl) return 'strike_not_found';
                let row = strikeEl.parentElement;
                for (let i = 0; i < 10; i++) {{
                    if (!row) return 'no_row';
                    const priceBtns = [...row.querySelectorAll('button')].filter(
                        b => /^\\$[0-9]/.test((b.textContent || '').trim())
                    );
                    if (priceBtns.length >= 2) {{
                        // Bid(Sell) is the FIRST price button (leftmost price column)
                        priceBtns[0].click();
                        return 'clicked_bid:' + priceBtns[0].textContent.trim();
                    }}
                    row = row.parentElement;
                }}
                return 'row_not_found';
            }}
        """)

    result = _click_bid_js(strike_label)
    print(f"  JS result: {result}")

    if not result or not str(result).startswith("clicked_bid"):
        # Scroll strike into view and retry
        try:
            page.locator(f'text="{strike_label}"').first.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(600)
            result = _click_bid_js(strike_label)
            print(f"  JS result after scroll: {result}")
        except Exception as e:
            print(f"  Scroll fallback failed: {e}")

    if not result or not str(result).startswith("clicked_bid"):
        snap(page, "sell_option_bid_not_found")
        raise RuntimeError(
            f"Could not find Bid(Sell) button for ${strike_int} {option_type.upper()} — "
            f"see data/screen_sell_option_bid_not_found.png"
        )

    page.wait_for_timeout(2000)
    snap(page, "sell_option_ticket_opened")
    return place_option_order(page, "sell", n_contracts, confirm)


def cmd_sell_option(args) -> None:
    from playwright.sync_api import sync_playwright

    result: dict = {
        "side": "sell", "submitted": False,
        "symbol": args.symbol, "option_type": args.option_type,
        "strike": args.strike, "expiry": args.expiry,
    }
    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            try:
                result = sell_option_from_portfolio(
                    page,
                    args.option_type,
                    float(args.strike),
                    args.contracts,
                    confirm=args.confirm,
                    expiry=args.expiry,
                )
                result.update({
                    "symbol": args.symbol, "option_type": args.option_type,
                    "strike": args.strike, "expiry": args.expiry,
                })
                safe_print(
                    f"\n[OK] Sell option: {args.contracts}x SPY ${args.strike} "
                    f"{args.option_type.upper()} {args.expiry}"
                )
            except Exception as e:
                safe_print(f"\n[ERROR] sell-option failed: {e}")
            finally:
                print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
    finally:
        _release_busy_lock()
    if not result.get("submitted") and args.confirm:
        sys.exit(1)


def cmd_keepalive(args) -> None:
    """
    Keep the Wealthsimple browser session alive.
    Navigates to WS home every 2 min; auto-logins if the session has expired.
    Backs off (skips cycle) while buy/sell is in progress (ws_busy.lock exists).
    Pass --once to run a single cycle and exit (useful for testing).
    """
    import time
    from datetime import datetime
    from playwright.sync_api import sync_playwright

    INTERVAL = 2 * 60  # 2 minutes
    once = getattr(args, "once", False)
    print(f"[keepalive] Starting - refresh every {INTERVAL // 60} min", flush=True)

    while True:
        if KEEPALIVE_LOCK.exists():
            print("[keepalive] Browser busy (buy/sell in progress) — skipping cycle", flush=True)
        else:
            try:
                with sync_playwright() as p:
                    try:
                        browser = p.chromium.connect_over_cdp(CDP_URL)
                    except Exception as exc:
                        safe_print(f"[keepalive] Could not connect to browser: {exc}")
                        browser = None

                    if browser is not None:
                        ctx = browser.contexts[0] if browser.contexts else None
                        if ctx is None:
                            print("[keepalive] No browser context found — is Chrome/Edge running?", flush=True)
                        else:
                            page = ctx.pages[0] if ctx.pages else ctx.new_page()
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"[keepalive] {ts} - refreshing WS home...", flush=True)
                            page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
                            page.wait_for_timeout(3000)

                            try:
                                on_login = is_login_page(page)
                            except Exception:
                                on_login = False

                            if on_login:
                                print("[keepalive] Session expired — auto-login...", flush=True)
                                ok = try_auto_login(page)
                                print(f"[keepalive] Auto-login: {'OK' if ok else 'FAILED'}", flush=True)
                            else:
                                print("[keepalive] Session active OK", flush=True)
            except Exception as exc:
                safe_print(f"[keepalive] Error: {exc}")

        if once:
            return
        time.sleep(INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wealthsimple browser automation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="First-time login: save session to data/ws_auth.json")
    sub.add_parser("balance", help="Fetch live balance from Wealthsimple")

    pos_p = sub.add_parser("position", help="Read actual position (fill price, qty) from WS")
    pos_p.add_argument("--symbol", required=True, help="e.g. NVDA")

    quote_p = sub.add_parser("quote", help="Get live price for a symbol from Wealthsimple (covers overnight ATS)")
    quote_p.add_argument("--symbol", required=True, help="e.g. AMC")
    quote_p.add_argument("--shares", type=float, default=None, help="Known share count for market-value method")

    buy_p = sub.add_parser("buy", help="Prepare a buy order")
    buy_p.add_argument("--symbol", required=True, help="e.g. SHOP or SHOP.TO")
    buy_p.add_argument("--shares", type=int, default=None)
    buy_p.add_argument("--price", type=float, default=None, help="Limit price (omit for Market)")
    buy_p.add_argument("--max-dollars", action="store_true", help="Buy in dollars and click Max")

    sell_p = sub.add_parser("sell", help="Prepare a sell order")
    sell_p.add_argument("--symbol", required=True)
    sell_p.add_argument("--shares", type=float, default=None, help="Exact share count (supports fractional)")
    sell_p.add_argument("--price", type=float, default=None, help="Limit price (omit for Market)")
    sell_p.add_argument("--sell-all", action="store_true", help="Click Max/Sell all on the sell ticket")

    ka_p = sub.add_parser("keepalive", help="Refresh WS session every 15 min; auto-login on expiry")
    ka_p.add_argument("--once", action="store_true", help="Run one cycle and exit (for testing)")

    def _add_option_args(p) -> None:
        p.add_argument("--symbol",      required=True,            help="Underlying symbol (e.g. SPY)")
        p.add_argument("--option-type", required=True,            choices=["call", "put"])
        p.add_argument("--strike",      required=True, type=int,  help="Strike price (e.g. 542)")
        p.add_argument("--expiry",      required=True,            help="Expiry date YYYY-MM-DD")
        p.add_argument("--contracts",   required=True, type=int,  help="Number of contracts")
        p.add_argument("--confirm",     action="store_true",      help="Submit the order (default: stop at review)")

    option_buy = sub.add_parser("buy-option",  help="Buy an options contract on Wealthsimple")
    _add_option_args(option_buy)
    option_buy.add_argument("--max-cost", type=float, default=None, help="Abort if reviewed debit exceeds this amount")
    _add_option_args(sub.add_parser("sell-option", help="Sell/close an options contract on Wealthsimple"))
    _add_option_args(sub.add_parser("option-quote", help="Read broker bid/ask for an option"))
    _add_option_args(sub.add_parser("option-position", help="Reconcile an option fill from broker state"))
    _add_option_args(sub.add_parser("cancel-option", help="Cancel pending SPY option order"))

    args = parser.parse_args()
    if args.cmd == "buy" and not args.max_dollars and args.shares is None:
        parser.error("buy requires --shares unless --max-dollars is used")
    if args.cmd == "sell" and not args.sell_all and args.shares is None:
        parser.error("sell requires --shares unless --sell-all is used")
    {
        "setup": cmd_setup, "buy": cmd_buy, "sell": cmd_sell,
        "balance": cmd_balance, "position": cmd_position,
        "keepalive": cmd_keepalive, "quote": cmd_quote,
        "buy-option": cmd_buy_option, "sell-option": cmd_sell_option,
        "option-quote": cmd_option_quote, "option-position": cmd_option_position,
        "cancel-option": cmd_cancel_option,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
