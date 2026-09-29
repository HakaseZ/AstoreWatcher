#!/usr/bin/env python3
"""M0 spike（一次性脚本，不参与正式实现）

目的：回答「容器里的浏览器能不能过 Akamai 拿到 Apple HK 库存数据」。

用法：
    python3 m0.py --tier 1        # headless shell，裸奔
    python3 m0.py --tier 2        # --headless=new + 指纹伪装（locale/时区/UA/反自动化 flag）
    python3 m0.py --tier 3        # headful，需 DISPLAY（容器内用 xvfb-run 包一层）

输出：stdout 一行 JSON 摘要；原始响应与截图写到 --outdir（默认 /data）。
"""

import argparse
import json
import os
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

BUY_PAGE = "https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro"
API_PATH = "/hk-zh/shop/fulfillment-messages"
API_ORIGIN = "https://www.apple.com"

# 2026-09 实测：Apple HK 已改用自家 shield（shld_bt_m / shld_bt_ck / sh_spksy），
# 页面上不再有 Akamai sensor，_abck/ak_bmsc/bm_sz 全程不下发 —— 保留旧 cookie 探测仅作对照
AKAMAI_COOKIES = ("_abck", "ak_bmsc", "bm_sz")
SHIELD_COOKIES = ("shld_bt_m", "shld_bt_ck", "sh_spksy")
ABCK_FAILED_PREFIX = "~-1~"

FETCH_JS = """
async ([path, body]) => {
  try {
    const res = await fetch(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
    const text = await res.text();
    return { ok: true, status: res.status, text };
  } catch (e) {
    return { ok: false, status: 0, text: String(e) };
  }
}
"""


def build_api_path(parts, location):
    params = [
        ("fae", "true"),
        ("pl", "true"),
        ("mts.0", "regular"),
        ("mts.1", "compact"),
    ]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("location", location)]
    return API_PATH + "?" + urllib.parse.urlencode(params)


def chrome_ua(executable):
    """从 chromium 二进制读主版本号，拼一个不带 Headless 字样的真实 UA。"""
    import re
    import subprocess

    try:
        out = subprocess.run(
            [executable or "chromium", "--version"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        major = re.search(r"(\d+)\.", out)
        ver = major.group(1) if major else "150"
    except Exception:
        ver = "150"
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"
    )


def chrome_full_version(executable):
    """返回 chromium 完整版本号，如 '150.0.7871.181'；失败退回 '150.0.0.0'。"""
    import re
    import subprocess

    try:
        out = subprocess.run(
            [executable or "chromium", "--version"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else "150.0.0.0"
    except Exception:
        return "150.0.0.0"


def apply_chrome_brand(page, executable):
    """CDP 覆盖 UA + Client Hints 元数据，让 Sec-CH-UA 与 UA 一致地自报 Google Chrome。"""
    import platform

    full = chrome_full_version(executable)
    major = full.split(".")[0]
    ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    grease = {"brand": "Not/A)Brand", "version": "8"}
    session = page.context.new_cdp_session(page)
    session.send("Network.setUserAgentOverride", {
        "userAgent": ua,
        "acceptLanguage": "zh-HK,zh-TW;q=0.9,en;q=0.8",
        "userAgentMetadata": {
            "brands": [grease,
                       {"brand": "Chromium", "version": major},
                       {"brand": "Google Chrome", "version": major}],
            "fullVersionList": [
                {"brand": "Not/A)Brand", "version": "8.0.0.0"},
                {"brand": "Chromium", "version": full},
                {"brand": "Google Chrome", "version": full}],
            "fullVersion": full,
            "platform": "Linux",
            "platformVersion": platform.uname().release,
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
        },
    })
    return ua


def launch_ctx(pw, tier, profile_dir, executable):
    """按档位启动 persistent context。返回 (ctx, launch_info)。"""
    # 容器内以 root 跑，必须 --no-sandbox
    stealth_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    common = {
        "user_data_dir": profile_dir,
        "locale": "zh-HK",
        "timezone_id": "Asia/Hong_Kong",
        "viewport": {"width": 1440, "height": 900},
        "args": stealth_args,
    }
    if executable:
        common["executable_path"] = executable
    info = {"executable": executable or "bundled"}
    if tier in (1, 2):
        # headless 模式下 UA 会带 "HeadlessChrome"，Akamai 一眼识破 → 换成同版本真实 Chrome UA
        common["user_agent"] = chrome_ua(executable)
        info["ua_override"] = common["user_agent"]
    if tier == 1:
        # 裸 headless：留作对照，预期最容易被识破
        ctx = pw.chromium.launch_persistent_context(headless=True, **common)
        info["mode"] = "headless"
    elif tier == 2:
        # headless + 完整指纹伪装（locale/时区/UA/反自动化 flag）
        ctx = pw.chromium.launch_persistent_context(headless=True, **common)
        info["mode"] = "headless+stealth"
    else:
        # headful：需要 DISPLAY，由 xvfb-run 提供
        ctx = pw.chromium.launch_persistent_context(headless=False, **common)
        info["mode"] = "headful"
    return ctx, info


def cookie_report(cookies):
    found = {c["name"]: c["value"] for c in cookies}
    out = {}
    for name in AKAMAI_COOKIES:
        val = found.get(name)
        out[name] = {
            "present": val is not None,
            "len": len(val) if val else 0,
            "head": (val[:40] if val else None),
        }
    abck = found.get("_abck")
    out["_abck_challenge_passed"] = bool(abck) and not abck.startswith(ABCK_FAILED_PREFIX)
    out["shield_cookies_present"] = all(name in found for name in SHIELD_COOKIES)
    return out


def summarize(text, part):
    """从响应文本里抽出判据字段。真实结构：body.content.pickupMessage.stores[].partsAvailability"""
    try:
        data = json.loads(text)
    except Exception:
        return {"parse": "failed", "raw": text[:300]}
    body = data.get("body", data)
    pm = (body.get("content") or {}).get("pickupMessage") or {}
    stores = pm.get("stores") or []
    per_store = []
    for s in stores:
        entry = (s.get("partsAvailability") or {}).get(part) or {}
        per_store.append({
            "store": s.get("storeName"),
            "pickupDisplay": entry.get("pickupDisplay"),
            "quote": entry.get("pickupSearchQuote"),
        })
    return {
        "parse": "ok",
        "stores_count": len(stores),
        "location": pm.get("location"),
        "part_present": any(x["pickupDisplay"] for x in per_store),
        "any_available": any(x["pickupDisplay"] == "available" for x in per_store),
        "per_store": per_store,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", type=int, choices=[1, 2, 3], default=2)
    ap.add_argument("--part", default="MJXV4ZA/A")
    ap.add_argument("--location", default="中環")
    ap.add_argument("--profile", default="/data/profile")
    ap.add_argument("--outdir", default="/data")
    ap.add_argument("--executable", default=os.environ.get("CHROMIUM_BIN", ""))
    ap.add_argument("--chrome-brand", action="store_true",
                    help="用 CDP 把 UA + Sec-CH-UA 整套覆盖成 Google Chrome")
    ap.add_argument("--reheat", action="store_true", default=True)
    args = ap.parse_args()

    os.makedirs(args.profile, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    api_path = build_api_path([args.part], args.location)

    result = {"tier": args.tier, "part": args.part, "location": args.location}
    attempts = []

    with sync_playwright() as pw:
        ctx, info = launch_ctx(pw, args.tier, args.profile, args.executable)
        result["launch"] = info
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if args.chrome_brand:
            result["chrome_brand_ua"] = apply_chrome_brand(page, args.executable)
        if os.environ.get("M0_LOG_HEADERS"):
            def _log_req(r):
                if "/fulfillment-messages" in r.url:
                    print("REQ", r.url, file=sys.stderr)
                    for k, v in sorted(r.headers.items()):
                        print(f"  {k}: {str(v)[:200]}", file=sys.stderr)
            page.on("request", _log_req)

        def goto_buy():
            page.goto(BUY_PAGE, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass  # networkidle 等不到不算失败，cookie 可能已下发

        def try_fetch():
            return page.evaluate(FETCH_JS, [api_path, None])

        goto_buy()
        result["ua"] = page.evaluate("() => navigator.userAgent")
        result["webdriver"] = page.evaluate("() => navigator.webdriver")
        result["cookies_after_goto"] = cookie_report(ctx.cookies())

        attempts.append(try_fetch())
        if os.environ.get("M0_RAW_FETCH"):
            # 现场对照：同一会话里用 bisect 式裸 fetch 再试一次
            result["raw_fetch_status"] = page.evaluate(
                "async (p) => (await fetch(p, {credentials:'same-origin'})).status",
                api_path,
            )
        if args.reheat and not (attempts[0]["ok"] and attempts[0]["status"] == 200):
            result["reheated"] = True
            goto_buy()
            attempts.append(try_fetch())

        try:
            page.screenshot(path=os.path.join(args.outdir, f"m0_tier{args.tier}.png"))
        except Exception as e:
            result["shot_error"] = str(e)[:200]
        ctx.close()

    result["attempts"] = []
    for a in attempts:
        rec = {"ok": a["ok"], "status": a["status"]}
        if a["status"] == 200:
            rec.update(summarize(a["text"], args.part))
        else:
            rec["raw"] = a["text"][:300]
        result["attempts"].append(rec)
        with open(os.path.join(args.outdir, "m0_last_response.txt"), "w") as f:
            f.write(a["text"])

    last = result["attempts"][-1]
    result["pass"] = bool(
        last.get("status") == 200
        and last.get("part_present")
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
