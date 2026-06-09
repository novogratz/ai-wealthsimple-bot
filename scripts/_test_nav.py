#!/usr/bin/env python3
"""Test search navigation on Wealthsimple — no order placed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wealthsimple_auto import open_browser, snap, try_auto_login, first_visible, click_first, WS_HOME
from playwright.sync_api import sync_playwright

search_term = sys.argv[1] if len(sys.argv) > 1 else "Thunderbird"
print(f"Searching Wealthsimple for: '{search_term}' — NO ORDER WILL BE PLACED")

with sync_playwright() as p:
    ctx, page = open_browser(p)

    page.goto(WS_HOME, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)

    if page.locator('input[type="password"]').first.is_visible(timeout=1500):
        print("Session expired — trying auto-login...")
        try_auto_login(page)

    snap(page, "test_home")
    print(f"Home URL: {page.url}")

    search = first_visible(page, [
        '[aria-label*="Search" i]',
        '[placeholder*="Search" i]',
        'input[type="search"]',
        '[data-testid*="search"]',
    ])
    if search is None:
        print("Search box not found — taking screenshot")
        snap(page, "test_no_search")
        sys.exit(1)

    search.click()
    page.wait_for_timeout(400)
    page.keyboard.type(search_term, delay=90)
    page.wait_for_timeout(2500)
    snap(page, "test_search_results")
    print(f"Typed '{search_term}' — screenshot saved")

    body = page.locator("body").inner_text()[:1000]
    print(f"\n--- Search results ---\n{body}\n---")
    print("DONE — no order placed.")
