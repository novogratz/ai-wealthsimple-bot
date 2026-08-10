import sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
from scripts.wealthsimple_auto import is_login_page, try_auto_login, WS_HOME

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://localhost:9222')
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    print('Navigating to Wealthsimple...')
    page.goto(WS_HOME, wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(3000)

    on_login = is_login_page(page)

    if on_login:
        print('Login page — auto-logging in...')
        ok = try_auto_login(page)
        if ok:
            print('SUCCESS')
        else:
            print('FAILED — 2FA or wrong credentials')
            raise SystemExit(1)
    else:
        print('Already logged in!')
