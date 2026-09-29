#!/usr/bin/env python3
"""M1：单次查询全部 32 个 SKU 的门店取货状态，打印结果后退出。

复用 M0 实测验证过的三件法宝（缺一会被 Apple shield 判机器人、返回 541）：
  1. persistent context + 挂载 profile —— 让 shield cookie 跨重启保留
  2. CDP 覆盖 UA + Client Hints —— Playwright 的 user_agent 参数改不了 Sec-CH-UA，
     而 Chromium 构建会自报 "Chromium"，与冒充 Chrome 的 UA 矛盾，必被拦
  3. 541 → 重新打开一次购买页 reheat（实测有效）

Apple 单批最多 15 个 part，32 个 SKU 拆 3 批（15/15/2）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

BUY_PAGE = "https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro"
API_PATH = "/hk-zh/shop/fulfillment-messages"
BATCH_SIZE = 15

FETCH_JS = """
async (path) => {
  try {
    const res = await fetch(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
    return { ok: true, status: res.status, text: await res.text() };
  } catch (e) {
    return { ok: false, status: 0, text: String(e) };
  }
}
"""


def chromium_version(executable):
    """返回 chromium 完整版本号，如 '150.0.7871.181'。"""
    try:
        out = subprocess.run([executable, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "150.0.0.0"


def apply_chrome_brand(page, executable):
    """CDP 整套覆盖 UA + Client Hints，让两者一致地自报 Google Chrome。"""
    import platform

    full = chromium_version(executable)
    major = full.split(".")[0]
    ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    session = page.context.new_cdp_session(page)
    session.send("Network.setUserAgentOverride", {
        "userAgent": ua,
        "acceptLanguage": "zh-HK,zh-TW;q=0.9,en;q=0.8",
        "userAgentMetadata": {
            "brands": [{"brand": "Not/A)Brand", "version": "8"},
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


def build_api_path(parts, location):
    params = [("fae", "true"), ("pl", "true"),
              ("mts.0", "regular"), ("mts.1", "compact")]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("location", location)]
    return API_PATH + "?" + urllib.parse.urlencode(params)


def query_batch(page, parts, location):
    """查一批（≤15）part，返回 (status, 解析后的 json)。"""
    raw = page.evaluate(FETCH_JS, build_api_path(parts, location))
    if raw["status"] != 200:
        return raw["status"], None
    try:
        return 200, json.loads(raw["text"])
    except Exception:
        return raw["status"], None


def parse_stores(data):
    """从响应里取出 门店 -> {part: (pickupDisplay, 文案)} 的映射。"""
    pm = ((data.get("body") or {}).get("content") or {}).get("pickupMessage") or {}
    stores = []
    for s in pm.get("stores") or []:
        avail = {}
        for part, entry in (s.get("partsAvailability") or {}).items():
            avail[part] = (entry.get("pickupDisplay"), entry.get("pickupSearchQuote"))
        stores.append({"name": s.get("storeName"), "parts": avail})
    return stores


def collect(profile, batches, location, executable):
    """跑一轮完整查询。返回 (每 part 的门店状态, 门店顺序, 失败批次)。"""
    stores_by_part = {}
    store_order = []
    failed = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=True,
            executable_path=executable,
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        f"(KHTML, like Gecko) Chrome/{chromium_version(executable).split('.')[0]}.0.0.0 Safari/537.36"),
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        apply_chrome_brand(page, executable)

        def open_buy_page():
            page.goto(BUY_PAGE, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass

        open_buy_page()

        for batch in batches:
            status, data = query_batch(page, batch, location)
            if status != 200:  # 541：重开一次购买页续命再试
                open_buy_page()
                status, data = query_batch(page, batch, location)
            if data is None:
                failed.append((batch, status))
                continue
            for store in parse_stores(data):
                if store["name"] not in store_order:
                    store_order.append(store["name"])
                for part in batch:
                    if part in store["parts"]:
                        stores_by_part.setdefault(part, {})[store["name"]] = store["parts"][part]
        ctx.close()

    return stores_by_part, store_order, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus", default="skus_hk.json")
    ap.add_argument("--location", default=os.environ.get("LOCATION", "中環"))
    ap.add_argument("--profile", default="/data/profile")
    ap.add_argument("--executable", default=os.environ.get("CHROMIUM_BIN", "chromium"))
    args = ap.parse_args()

    with open(args.skus, encoding="utf-8") as f:
        skus = json.load(f)
    parts = [s["part"] for s in skus]
    meta = {s["part"]: s for s in skus}
    batches = [parts[i:i + BATCH_SIZE] for i in range(0, len(parts), BATCH_SIZE)]

    stores_by_part, store_order, failed = collect(
        args.profile, batches, args.location, args.executable)

    # 一次都没查成功：多半是冷启动被判机器人，且该 profile 已被污染（会持续 541）。
    # 实测有效的解法是丢掉 profile 重来一次。
    if len(failed) == len(batches):
        print(f"全部批次失败（HTTP {failed[0][1]}），丢弃 profile 重建后重试…", file=sys.stderr)
        shutil.rmtree(args.profile, ignore_errors=True)
        stores_by_part, store_order, failed = collect(
            args.profile, batches, args.location, args.executable)

    print(f"地点：{args.location}　门店数：{len(store_order)}　SKU：{len(parts)}")
    for part in parts:
        m = meta[part]
        label = f"{m['model']} {m['capacity']} {m['color']}"
        per_store = stores_by_part.get(part) or {}
        if not per_store:
            print(f"\n{label} ({part})\n  未取到数据")
            continue
        picks = [(name, v) for name, v in per_store.items() if v[0] == "available"]
        print(f"\n{label} ({part})")
        for name in store_order:
            disp, quote = per_store.get(name, (None, None))
            mark = "✅" if disp == "available" else "  "
            print(f"  {mark} {name:<18} {quote or disp or '?'}")
        if picks:
            print(f"  → 可取貨：{', '.join(n for n, _ in picks)}")

    if failed:
        print("\n以下批次查询失败：", file=sys.stderr)
        for batch, status in failed:
            print(f"  HTTP {status}：{batch[0]} … {batch[-1]}（{len(batch)} 个）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
