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
WS_HOME = "https://my.wealthsimple.com/app/home"

DATA.mkdir(exist_ok=True)


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
    click_first(page, [
        'button:has-text("Max")',
        '[role="button"]:has-text("Max")',
        'text="Max"',
    ], timeout=3000)
    page.wait_for_timeout(700)


def use_max_shares(page) -> None:
    print("Using Max shares / sell all...")
    clicked = click_first(page, [
        'button:has-text("Max")',
        '[role="button"]:has-text("Max")',
        'button:has-text("Sell all")',
        'button:has-text("Sell All")',
        '[role="button"]:has-text("Sell all")',
        '[role="button"]:has-text("Sell All")',
        'text="Max"',
        'text="Sell all"',
        'text="Sell All"',
    ], timeout=4000)
    if clicked:
        page.wait_for_timeout(700)
        return

    # Some Wealthsimple tickets expose a "Shares" amount input with a Max pill
    # inside the same control. If selector clicks fail, click near the right edge
    # of the visible input where the Max action usually appears.
    el = page.locator("input:visible").first
    el.wait_for(state="visible", timeout=3000)
    box = el.bounding_box()
    if not box:
        raise RuntimeError("Could not locate sell quantity input for Max shares.")
    page.mouse.click(box["x"] + box["width"] - 28, box["y"] + box["height"] / 2)
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


def cmd_balance(_args) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, ctx, page = open_browser(p)
        try:
            balance = get_live_balance(page)
            if balance is not None:
                print(f"LIVE_BALANCE_CAD:{balance:.2f}")
                print(f"[OK] Live balance fetched: ${balance:.2f} CAD")
            else:
                print("[ERROR] Could not find balance on page.")
                snap(page, "balance_fail")
                sys.exit(1)
        finally:
            browser.close()


def open_browser(p):
    if not AUTH.exists():
        print(f"No session file at {AUTH}.")
        print("Run first: python scripts/wealthsimple_auto.py setup")
        sys.exit(1)
    browser = p.firefox.launch(headless=False, slow_mo=60)
    ctx = browser.new_context(storage_state=str(AUTH))
    page = ctx.new_page()
    return browser, ctx, page


def save_session(ctx) -> None:
    ctx.storage_state(path=str(AUTH))


def navigate_to_stock(page, ws_symbol: str) -> None:
    print("Loading Wealthsimple home...")
    page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)
    snap(page, "home")

    if page.locator('input[type="password"], input[placeholder*="Password" i]').first.is_visible(timeout=1000):
        raise RuntimeError("Wealthsimple session expired - run: python scripts/wealthsimple_auto.py setup")

    print(f"Searching for {ws_symbol}...")
    search = first_visible(page, [
        '[aria-label*="Search" i]',
        '[placeholder*="Search" i]',
        'input[type="search"]',
        '[data-testid*="search"]',
    ])
    if search is None:
        snap(page, "search_not_found")
        raise RuntimeError("Search box not found - see data/screen_search_not_found.png")

    search.click()
    page.wait_for_timeout(400)
    page.keyboard.type(ws_symbol, delay=90)
    page.wait_for_timeout(2200)
    snap(page, "search_results")

    clicked = click_first(page, [
        '[data-testid*="search-result"]',
        '[data-testid*="result"]',
        'a[href*="/app/trade"]',
        'a[href*="/stock"]',
        '[class*="SearchResult"]',
        '[class*="search-result"]',
    ])
    if not clicked:
        print("  Falling back to Enter key...")
        page.keyboard.press("Enter")

    page.wait_for_timeout(4000)
    snap(page, "stock_page")


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

    # Find the Non-registered account with the highest CAD balance.
    # There are often multiple Non-registered accounts — pick the richest one.
    # Click by pixel coordinates at the right edge of the row (where the radio button is).
    account_labels = ["Non-registered", "Unregistered", "Personal"]
    best_balance = -1.0
    best_box = None

    for label in account_labels:
        try:
            els = page.locator(
                f'[role="option"]:has-text("{label}"), li:has-text("{label}"), button:has-text("{label}")'
            ).all()
            for el in els:
                try:
                    if not el.is_visible(timeout=500):
                        continue
                    text = el.inner_text()
                    m = re.search(r'\$([0-9,]+\.?\d*)\s*CAD', text)
                    bal = float(m.group(1).replace(",", "")) if m else 0.0
                    if bal > best_balance:
                        box = el.bounding_box()
                        if box:
                            best_balance = bal
                            best_box = box
                except Exception:
                    pass
        except Exception:
            pass

    if best_box is not None:
        # Click near the right edge of the row — that's where the radio button circle lives
        x = best_box["x"] + best_box["width"] - 20
        y = best_box["y"] + best_box["height"] / 2
        page.mouse.click(x, y)
        print(f"  Clicked radio button for Non-registered ${best_balance:.2f} CAD account")
        page.wait_for_timeout(700)
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
    clicked = click_first(page, [
        f'button:has-text("{side.title()}")',
        f'[role="tab"]:has-text("{side.title()}")',
        f'[data-testid*="{side}"]',
    ], timeout=5000)
    if not clicked:
        snap(page, f"{side}_tab_fail")
        raise RuntimeError(f"Could not find {side.title()} tab - see data/screen_{side}_tab_fail.png")
    page.wait_for_timeout(600)
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
    page.get_by_role("button", name="Next").click(timeout=8000)
    page.wait_for_timeout(3000)
    snap(page, f"{side}_review")
    submitted = False

    if confirm:
        print("Placing order (confirm)...")
        confirmed = click_first(page, [
            'button:has-text("Place order")',
            'button:has-text("Place Order")',
            'button:has-text("Confirm")',
            'button:has-text("Submit")',
            '[data-testid*="confirm"]',
            '[data-testid*="submit"]',
        ], timeout=6000)
        if not confirmed:
            snap(page, f"{side}_confirm_fail")
            raise RuntimeError(f"Confirm button not found - see data/screen_{side}_confirm_fail.png")
        page.wait_for_timeout(2500)
        snap(page, f"{side}_done")
        print("  Order submitted.")
        submitted = True
    else:
        print("  Stopped at review page - confirm manually.")

    return parse_review_details(page, side, submitted)


def cmd_setup(_args) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False, slow_mo=50)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(WS_HOME)
        print()
        print("A Firefox window has opened.")
        print("Log in to Wealthsimple, navigate to your home page,")
        print("then come back here and press ENTER.")
        input()
        save_session(ctx)
        snap(page, "setup_done")
        browser.close()
    print(f"Session saved -> {AUTH}")


def cmd_buy(args) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, ctx, page = open_browser(p)
        try:
            navigate_to_stock(page, strip_exchange(args.symbol))
            result = place_order(
                page,
                "buy",
                args.shares,
                args.price,
                confirm=True,
                max_dollars=args.max_dollars,
            )
            label = "Dollars Max (Market)" if args.max_dollars else f"{args.shares} shares (Market)"
            if args.price:
                label = f"{args.shares} shares @ ${args.price:.2f}"
            print()
            print(f"[OK] Buy order: {args.symbol} {label}")
            result["symbol"] = args.symbol
            print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
        finally:
            save_session(ctx)
            browser.close()


def cmd_sell(args) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, ctx, page = open_browser(p)
        try:
            navigate_to_stock(page, strip_exchange(args.symbol))
            result = place_order(page, "sell", args.shares, None, confirm=True, sell_all=args.sell_all)
            print()
            label = "all shares" if args.sell_all else f"{args.shares} shares"
            print(f"[OK] Sell order: {label} x {args.symbol} (Market)")
            result["symbol"] = args.symbol
            print("ORDER_RESULT_JSON:" + json.dumps(result, sort_keys=True))
        finally:
            save_session(ctx)
            browser.close()


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

    args = parser.parse_args()
    if args.cmd == "buy" and not args.max_dollars and args.shares is None:
        parser.error("buy requires --shares unless --max-dollars is used")
    if args.cmd == "sell" and not args.sell_all and args.shares is None:
        parser.error("sell requires --shares unless --sell-all is used")
    {"setup": cmd_setup, "buy": cmd_buy, "sell": cmd_sell, "balance": cmd_balance}[args.cmd](args)


if __name__ == "__main__":
    main()
