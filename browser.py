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
import time
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
    """返回 chromium 完整版本号，如 '150.0.7871.181'。

    取不到版本一律抛出，不再静默回退到一个假版本号：假版本号会让 UA / Client Hints
    里自报的 Google Chrome 版本与真实 Chromium 不符，反而更容易被 shield 判机器人，
    同时也会掩盖「可执行文件坏了」的真实故障（由 worker 的预检先行报销更明确的错误）。
    """
    try:
        out = subprocess.run([executable, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except OSError as e:  # 文件不存在 / 不可执行
        raise RuntimeError(f"无法执行 {executable}：{e}") from e
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out or "")
    if not m:
        raise RuntimeError(f"无法从 {executable} 的输出解析版本号：{out!r}")
    return m.group(1)


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
        self._ensure_page()
        raw = self.page.evaluate(FETCH_JS, build_api_path(parts, self.location))
        if raw["status"] != 200:
            return raw["status"], None
        try:
            return 200, parse_snapshot(json.loads(raw["text"]))
        except Exception:
            return raw["status"], None

    def _ensure_page(self):
        """取数前确认页面仍然可用（B5）。

        浏览器自己可能把页面关掉（崩溃恢复、内存回收）或导航走（风控跳转），
        此时 evaluate 会失败或拿到空数据。这里重建页面并重新做一次 CDP brand 覆盖
        ——注意 new_page 不会继承上一次的覆盖——再重开购买页完成 shield 校验。
        """
        if self.ctx is None:
            raise RuntimeError("浏览器上下文已释放，无法取数")
        page = self.page
        if page is not None and not page.is_closed():
            return
        self.page = self.ctx.new_page()
        self._apply_chrome_brand()
        self.open_buy_page()

    def fetch_all(self, parts):
        """把 parts 按 15 个一批查完。返回 (合并后的 snapshot, 失败批次列表)。

        单批失败会先 reheat 再试一次；仍失败则记入 failed。每批取数前都会确认
        页面有效（见 _ensure_page）。
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
            # 回收可能残留的 chromium 孙进程。worker 进程已设为 subreaper，chromium
            # 退出后会 reparent 到本进程，这里 waitpid 一下避免它们变成僵尸无限累积
            # （撑满 PID cgroup 后连 fork 都失败，表现为反复 START_FAILED）。
            self._reap_children()
            self.ctx = None
            self._pw = None

    @staticmethod
    def _reap_children():
        """回收本进程下已死的子/孙进程（chromium 崩溃后残留的僵尸）。

        仅 best-effort：waitpid(-1, WNOHANG) 只回收「已经死掉」的进程，
        活着的 chromium 不会被误伤；没有任何可回收子进程时 ChildProcessError 被忽略。
        """
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass

    def rebuild_profile(self):
        """把疑似损坏的 profile 目录移走备份（冷启动持续 541 时必须丢弃重建）。

        只能在 close() 之后调用。**改为重命名而不是直接删除**：profile 里的 shield
        cookie 很珍贵，重建有被判机器人的风险，留一份备份给人工捞回的机会。
        """
        if not os.path.isdir(self.profile):
            return
        backup = f"{self.profile}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
        try:
            os.replace(self.profile, backup)  # 同目录 rename，原子
        except OSError:  # 跨设备 / 目标已存在等：退化为原行为
            shutil.rmtree(self.profile, ignore_errors=True)
