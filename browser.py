"""浏览器会话封装 —— M0 实测验证过的取数配方，勿随意改动。

三件缺一不可的法宝（少一件就会被 Apple shield 判机器人、返回 541）：
  1. persistent context + 挂载 profile —— 让 shield cookie 跨重启保留
  2. CDP 覆盖 UA + Client Hints —— Playwright 的 user_agent 参数改不了 Sec-CH-UA，
     而 Chromium 构建会自报 "Chromium"，与冒充 Chrome 的 UA 自相矛盾，必被拦
  3. 541 → 重新打开一次购买页 reheat（实测有效）

另一个坑：新 profile 若首次就被判机器人，该 profile 会**持续** 541，
此时必须丢弃重建（由调用方决定是否重建，见 BrowserSession.rebuild_profile）。
"""

import json
import os
import platform
import re
import shutil
import subprocess
import urllib.parse

from playwright.sync_api import sync_playwright

BUY_PAGE = "https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro"
API_PATH = "/hk-zh/shop/fulfillment-messages"
BATCH_SIZE = 15  # Apple 单批上限，超过会静默丢弃

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


def build_api_path(parts, location):
    params = [("fae", "true"), ("pl", "true"),
              ("mts.0", "regular"), ("mts.1", "compact")]
    params += [(f"parts.{i}", p) for i, p in enumerate(parts)]
    params += [("location", location)]
    return API_PATH + "?" + urllib.parse.urlencode(params)


def parse_snapshot(data):
    """响应 JSON → {part: {store: {"display", "quote"}}}。"""
    pm = ((data.get("body") or {}).get("content") or {}).get("pickupMessage") or {}
    snapshot = {}
    for store in pm.get("stores") or []:
        name = store.get("storeName")
        if not name:
            continue
        for part, entry in (store.get("partsAvailability") or {}).items():
            snapshot.setdefault(part, {})[name] = {
                "display": entry.get("pickupDisplay"),
                "quote": entry.get("pickupSearchQuote"),
            }
    return snapshot


class BrowserSession:
    """持有 persistent context 的一次浏览器会话。用完必须 close()。"""

    def __init__(self, profile, location="中環",
                 executable=None, headless=True):
        self.profile = profile
        self.location = location
        self.executable = executable or os.environ.get("CHROMIUM_BIN", "chromium")
        self.headless = headless
        self._pw = None
        self.ctx = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile,
            headless=self.headless,
            executable_path=self.executable,
            user_agent=self._ua(),
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._apply_chrome_brand()
        self.open_buy_page()
        return self

    def _ua(self):
        major = chromium_version(self.executable).split(".")[0]
        return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")

    def _apply_chrome_brand(self):
        """CDP 整套覆盖 UA + Client Hints，让两者一致地自报 Google Chrome。"""
        full = chromium_version(self.executable)
        major = full.split(".")[0]
        session = self.ctx.new_cdp_session(self.page)
        session.send("Network.setUserAgentOverride", {
            "userAgent": self._ua(),
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

    def open_buy_page(self):
        """打开购买页完成 shield 校验、拿到 cookie；同时用作 541 之后的 reheat。"""
        self.page.goto(BUY_PAGE, wait_until="domcontentloaded", timeout=60000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

    def query(self, parts):
        """查一批（≤15）part。返回 (HTTP 状态码, snapshot 或 None)。"""
        raw = self.page.evaluate(FETCH_JS, build_api_path(parts, self.location))
        if raw["status"] != 200:
            return raw["status"], None
        try:
            return 200, parse_snapshot(json.loads(raw["text"]))
        except Exception:
            return raw["status"], None

    def fetch_all(self, parts):
        """把 parts 按 15 个一批查完。返回 (合并后的 snapshot, 失败批次列表)。

        单批失败会先 reheat 再试一次；仍失败则记入 failed。
        """
        batches = [parts[i:i + BATCH_SIZE] for i in range(0, len(parts), BATCH_SIZE)]
        snapshot = {}
        failed = []
        for batch in batches:
            status, data = self.query(batch)
            if data is None:  # 541：重开购买页续命再试
                self.open_buy_page()
                status, data = self.query(batch)
            if data is None:
                failed.append({"parts": batch, "status": status})
                continue
            for part, stores in data.items():
                snapshot.setdefault(part, {}).update(stores)
        return snapshot, failed

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
            self.ctx = None
            self._pw = None

    def rebuild_profile(self):
        """丢弃被污染的 profile 目录（冷启动 541 后会持续失败）。只能在 close() 之后调用。"""
        shutil.rmtree(self.profile, ignore_errors=True)
