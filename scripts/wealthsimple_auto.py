#!/usr/bin/env python3
"""
Wealthsimple browser automation via Playwright.

Default behavior stops at the review page. Passing --confirm submits a real order.
"""
import argparse
import json
import re
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

# Use the real system Edge — stable, familiar UI, not "Chrome for Testing"
EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DATA.mkdir(exist_ok=True)


def _acquire_busy_lock() -> None:
    KEEPALIVE_LOCK.write_text("busy")


def _release_busy_lock() -> None:
    KEEPALIVE_LOCK.unlink(missing_ok=True)


def snap(page, tag: str) -> Path:
    path = DATA / f"screen_{tag}.png"
    page.screenshot(path=str(path))
    print(f"  [snap] {path.name}")
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
        page.keyboard.press("Control+A")
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
    page.keyboard.press("Control+A")
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
        return
    except Exception:
        pass
    raise RuntimeError("Max button not found — account may not be selected yet")


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


def parse_review_details(page, side: str, submitted: bool) -> dict:
    text = page.locator("body").inner_text(timeout=5000)

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

    # Try multiple strategies to find the balance
    text = page.locator("body").inner_text()

    # Strategy 1: Look for "Unregistered" followed by a dollar amount
    # Patterns to try:
    patterns = [
        r"Unregistered.*?\$([0-9,.]+)",
        r"Non-registered.*?\$([0-9,.]+)",
        r"Personal.*?\$([0-9,.]+)",
        r"Available to trade.*?\$([0-9,.]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            val = parse_money(match.group(1))
            if val is not None and val > 0:
                print(f"  Found balance using pattern '{pattern}': ${val}")
                return val

    # Strategy 2: Look for specific data-testids or roles if available
    try:
        # Wealthsimple often uses specific components for account rows
        rows = page.locator('[data-testid*="account-row"], [role="link"]:has-text("Unregistered")').all()
        for row in rows:
            row_text = row.inner_text()
            if "Unregistered" in row_text or "Non-registered" in row_text:
                match = re.search(r"\$([0-9,.]+)", row_text)
                if match:
                    val = parse_money(match.group(1))
                    if val is not None:
                        print(f"  Found balance in account row: ${val}")
                        return val
    except Exception:
        pass

    return None


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
        # Fill email
        email_input = first_visible(page, [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete*="email"]',
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

        still_on_login = page.locator(
            'input[type="password"], input[placeholder*="Password" i]'
        ).first.is_visible(timeout=2000)

        if still_on_login:
            print("  Auto-login: still on login page — check credentials or 2FA required")
            return False

        print("  Auto-login: success!")
        return True

    except Exception as e:
        print(f"  Auto-login error: {e}")
        return False


def cmd_balance(_args) -> None:
    from playwright.sync_api import sync_playwright

    _acquire_busy_lock()
    try:
        with sync_playwright() as p:
            ctx, page = open_browser(p)
            page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)

            if page.locator('input[type="password"], input[placeholder*="Password" i]').first.is_visible(timeout=1500):
                if not try_auto_login(page):
                    print("SESSION_EXPIRED: Wealthsimple session expired — run: python scripts/wealthsimple_auto.py setup")
                    sys.exit(1)
                page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)

            balance = get_live_balance(page)
            if balance is not None:
                print(f"LIVE_BALANCE_CAD:{balance:.2f}")
                print(f"[OK] Live balance fetched: ${balance:.2f} CAD")
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
    print("No running browser found. Run first: python scripts/wealthsimple_auto.py setup")
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

    if page.locator('input[type="password"], input[placeholder*="Password" i]').first.is_visible(timeout=1000):
        if not try_auto_login(page):
            raise RuntimeError("Wealthsimple session expired — run: python scripts/wealthsimple_auto.py setup")
        page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)

    # Navigate directly to the trade URL — much more reliable than search UI
    print(f"Navigating to {ws_symbol} trade page...")
    ctx = page.context
    trade_url = f"https://my.wealthsimple.com/app/trade/{ws_symbol}"
    page.goto(trade_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)

    # If WS redirected away (e.g. to home or search), fall back to search UI
    if ws_symbol.upper() not in page.url.upper():
        print(f"  Direct URL redirected to {page.url} — trying search...")
        page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)

        search = first_visible(page, [
            '[aria-label*="Search" i]',
            '[placeholder*="Search" i]',
            'input[type="search"]',
            '[data-testid*="search"]',
        ])
        if search is None:
            snap(page, "search_not_found")
            raise RuntimeError("Search box not found")

        # Track pages before so we can catch a new tab opening
        pages_before = set(id(p) for p in ctx.pages)
        search.click()
        page.wait_for_timeout(400)
        page.keyboard.type(ws_symbol, delay=90)
        page.wait_for_timeout(2200)
        snap(page, "search_results")

        clicked = click_first(page, [
            '[data-testid*="search-result"]',
            '[data-testid*="result"]',
            f'a[href*="{ws_symbol}"]',
            'a[href*="/app/trade"]',
            '[class*="SearchResult"]',
            '[class*="search-result"]',
        ])
        if not clicked:
            page.keyboard.press("Enter")

        page.wait_for_timeout(3000)

        # If a new tab opened, switch to it
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

    # Use JS to directly click the first Non-registered / Unregistered option in the DOM.
    # Coordinate-based clicks land on elements behind the picker overlay.
    for label in ["Non-registered", "Unregistered", "Personal"]:
        found = page.evaluate(f"""
            () => {{
                const all = [
                    ...document.querySelectorAll('[role="option"]'),
                    ...document.querySelectorAll('li'),
                    ...document.querySelectorAll('button'),
                ];
                for (const el of all) {{
                    if (el.textContent.trim().startsWith('{label}') || el.innerText?.includes('{label}')) {{
                        const radio = el.querySelector('input[type="radio"]');
                        if (radio) {{ radio.click(); return true; }}
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }}
        """)
        if found:
            page.wait_for_timeout(700)
            snap(page, f"{side}_acct_selected")
            print(f"  Selected {label} account via JS click")
            return

    snap(page, f"{side}_acct_fail")
    raise RuntimeError(f"Unregistered account not found - see data/screen_{side}_acct_fail.png")


def place_order(
    page,
    side: str,
    shares: Optional[int],
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

    if price is not None and side == "buy":
        print("Switching to Limit order...")
        try:
            page.locator('button:has-text("Market")').first.click(timeout=3000)
            page.wait_for_timeout(400)
            page.locator(
                'li:has-text("Limit"), button:has-text("Limit"), [role="option"]:has-text("Limit")'
            ).first.click(timeout=3000)
            page.wait_for_timeout(600)
            snap(page, "limit_selected")
            print(f"Entering limit price ${price:.2f}...")
            fill_visible_input(page, 0, f"{price:.2f}")
            page.wait_for_timeout(300)
        except PWTimeout:
            print("  Warning: could not switch to Limit - proceeding as Market")
            price = None
    else:
        print("Keeping Market order type.")

    choose_unregistered_account(page, side)

    if max_dollars and side == "buy":
        print("Using Dollars -> Max...")
        use_max_dollars(page)
        snap(page, f"{side}_max")
    elif sell_all and side == "sell":
        use_max_shares(page)
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
    submitted = False

    if confirm:
        print("Placing order (confirm)...")
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
            print(f"  Submit clicked via: {submitted_via_js}")
        else:
            debug_text = (submitted_via_js or "").replace("not_found::", "")[:1000] if submitted_via_js else "no text"
            print(f"  Page text at submit: {debug_text[:500]}")
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
        return parse_review_details(page, side, submitted)
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

    subprocess.Popen([
        EDGE_EXE,
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
    print("  1. A Microsoft Edge window just opened.")
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
    print("  Done. Chrome will stay open — the bot connects to it for all operations.")
    print(f"  Keep that Chrome window running in the background.")


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
                print(f"\n[OK] Buy order: {args.symbol} {label}")
            except Exception as e:
                print(f"\n[ERROR] Buy failed: {e}")
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
                result = place_order(page, "sell", args.shares, None, confirm=True, sell_all=args.sell_all)
                result["symbol"] = args.symbol
                label = "all shares" if args.sell_all else f"{args.shares} shares"
                print(f"\n[OK] Sell order: {label} x {args.symbol} (Market)")
            except Exception as e:
                print(f"\n[ERROR] Sell failed: {e}")
            finally:
                print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
    finally:
        _release_busy_lock()
    if not result.get("submitted"):
        sys.exit(1)


def cmd_keepalive(args) -> None:
    """
    Keep the Wealthsimple browser session alive.
    Navigates to WS home every 15 min; auto-logins if the session has expired.
    Backs off (skips cycle) while buy/sell is in progress (ws_busy.lock exists).
    Pass --once to run a single cycle and exit (useful for testing).
    """
    import time
    from datetime import datetime
    from playwright.sync_api import sync_playwright

    INTERVAL = 15 * 60  # 15 minutes
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
                        print(f"[keepalive] Could not connect to browser: {exc}", flush=True)
                        browser = None

                    if browser is not None:
                        ctx = browser.contexts[0] if browser.contexts else None
                        if ctx is None:
                            print("[keepalive] No browser context found — is Edge running?", flush=True)
                        else:
                            page = ctx.pages[0] if ctx.pages else ctx.new_page()
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"[keepalive] {ts} - refreshing WS home...", flush=True)
                            page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
                            page.wait_for_timeout(3000)

                            try:
                                on_login = page.locator(
                                    'input[type="password"], input[placeholder*="Password" i]'
                                ).first.is_visible(timeout=2000)
                            except Exception:
                                on_login = False

                            if on_login:
                                print("[keepalive] Session expired — auto-login...", flush=True)
                                ok = try_auto_login(page)
                                print(f"[keepalive] Auto-login: {'OK' if ok else 'FAILED'}", flush=True)
                            else:
                                print("[keepalive] Session active OK", flush=True)
            except Exception as exc:
                print(f"[keepalive] Error: {exc}", flush=True)

        if once:
            return
        time.sleep(INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wealthsimple browser automation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="First-time login: save session to data/ws_auth.json")
    sub.add_parser("balance", help="Fetch live balance from Wealthsimple")

    buy_p = sub.add_parser("buy", help="Prepare a buy order")
    buy_p.add_argument("--symbol", required=True, help="e.g. SHOP or SHOP.TO")
    buy_p.add_argument("--shares", type=int, default=None)
    buy_p.add_argument("--price", type=float, default=None, help="Limit price (omit for Market)")
    buy_p.add_argument("--max-dollars", action="store_true", help="Buy in dollars and click Max")

    sell_p = sub.add_parser("sell", help="Prepare a sell order")
    sell_p.add_argument("--symbol", required=True)
    sell_p.add_argument("--shares", type=int, default=None)
    sell_p.add_argument("--sell-all", action="store_true", help="Click Max/Sell all on the sell ticket")

    ka_p = sub.add_parser("keepalive", help="Refresh WS session every 15 min; auto-login on expiry")
    ka_p.add_argument("--once", action="store_true", help="Run one cycle and exit (for testing)")

    args = parser.parse_args()
    if args.cmd == "buy" and not args.max_dollars and args.shares is None:
        parser.error("buy requires --shares unless --max-dollars is used")
    if args.cmd == "sell" and not args.sell_all and args.shares is None:
        parser.error("sell requires --shares unless --sell-all is used")
    {"setup": cmd_setup, "buy": cmd_buy, "sell": cmd_sell, "balance": cmd_balance, "keepalive": cmd_keepalive}[args.cmd](args)


if __name__ == "__main__":
    main()
