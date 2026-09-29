#!/usr/bin/env python3
"""bisect 基准：已知能过 shield 的最小实现。与 m0.py 逐项变形对照用。"""
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
P = '/hk-zh/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&mts.1=compact&parts.0=MJXV4ZA/A&location=%E4%B8%AD%E7%92%B0'

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir="/data/profile_bisectF", headless=True,
        executable_path="/usr/bin/chromium",
        user_agent=UA, locale="zh-HK", timezone_id="Asia/Hong_Kong",
        viewport={"width": 1440, "height": 900},
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.setUserAgentOverride", {
        "userAgent": UA, "acceptLanguage": "zh-HK,zh-TW;q=0.9,en;q=0.8",
        "userAgentMetadata": {
            "brands": [{"brand": "Not/A)Brand", "version": "8"},
                       {"brand": "Chromium", "version": "150"},
                       {"brand": "Google Chrome", "version": "150"}],
            "fullVersionList": [{"brand": "Not/A)Brand", "version": "8.0.0.0"},
                       {"brand": "Chromium", "version": "150.0.7871.181"},
                       {"brand": "Google Chrome", "version": "150.0.7871.181"}],
            "fullVersion": "150.0.7871.181",
            "platform": "Linux", "platformVersion": "6.1.0",
            "architecture": "x86", "model": "", "mobile": False, "bitness": "64",
        },
    })
    page.goto("https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(10000)
    print("fetch:", page.evaluate(f"async () => (await fetch('{P}', {{credentials:'same-origin'}})).status"))
    ctx.close()
