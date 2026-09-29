#!/usr/bin/env python3
"""poller.run_once 的边界逻辑集成测试（无浏览器 / 无网络）。

通过把 browser 模块替换成桩，直接验证：
- 首次加入 → 全量推送；之后只推变化
- edge A：已初始化目标新增 SKU → 只推该 SKU 当前状态，不重推已有 SKU
- edge B：禁用 → 清除 init 标记；重新启用 → 再次全量推送
- 必填守卫：parts 或 stores 为空的启用目标被跳过
- 变动推送标题为「您关注的 {型号} 监测到库存变化」并按型号分组
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

# 在导入 poller 之前装好 browser 桩，避免真实 playwright 依赖
_STORES = ["S0", "S1", "S2"]

_AVAIL = {}  # {(part, store): "available"|"unavailable"}，由用例动态修改


def _make_snapshot(parts):
    snap = {}
    for p in parts:
        snap[p] = {}
        for s in _STORES:
            disp = _AVAIL.get((p, s), "unavailable")
            snap[p][s] = {"display": disp,
                          "quote": "備妥於：今日" if disp == "available" else "暫無供應"}
    return snap


_fake_browser = types.ModuleType("browser")


class FakeSession:
    def __init__(self, profile, location=None, executable=None):
        self.profile = profile
        self.location = location

    def start(self):
        pass

    def close(self):
        pass

    def rebuild_profile(self):
        pass

    def fetch_all(self, parts):
        return _make_snapshot(parts), []


_fake_browser.BrowserSession = FakeSession
sys.modules["browser"] = _fake_browser

import poller  # noqa: E402
import notifier  # noqa: E402
import state as state_mod  # noqa: E402

SKU_META = {
    "P1": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
    "P2": {"model": "iPhone 18 Pro", "color": "黑色", "capacity": "256GB"},
}


class PollerEdgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.state_path = os.path.join(self.data, "state.json")
        self.init_path = os.path.join(self.data, "init.json")
        self.profile = os.path.join(self.data, "profile")
        _AVAIL.clear()
        _AVAIL[("P1", "S0")] = "available"  # 初始：P1 在 S0 有货
        self.captured = []
        notifier.send = lambda url, title, body, **kw: (self.captured.append((url, title, body)) or (True, "ok"))

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, targets):
        return {"location": "中環", "interval_sec": 120, "targets": targets}

    def _target(self, tid, parts, stores, enabled=True, notify_on=None, push_mode="merged"):
        return {"id": tid, "name": tid, "bark_url": "https://bark/" + tid,
                "enabled": enabled, "parts": parts, "stores": stores,
                "notify_on": notify_on or ["available", "unavailable"], "push_mode": push_mode}

    def _run(self, cfg):
        return poller.run_once(cfg, SKU_META, self.profile,
                                self.state_path, self.init_path)

    # ---- 1. 首次全量 + 之后只推变化 ----
    def test_first_full_then_only_changes(self):
        t = self._target("t1", ["P1"], ["S0", "S1"])
        self._run(self._cfg([t]))
        # 首次：一条「正在为您监测 iPhone 库存」全量推送
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0][1], "正在为您监测 iPhone 库存，当前库存如下")

        # 状态未变 → 不再推送
        self._run(self._cfg([t]))
        self.assertEqual(len(self.captured), 1)

        # P1 在 S1 新补货 → 推一条变动（标题为型号）
        _AVAIL[("P1", "S1")] = "available"
        self._run(self._cfg([t]))
        self.assertEqual(len(self.captured), 2)
        title, body = self.captured[-1][1], self.captured[-1][2]
        self.assertEqual(title, "您关注的 iPhone 18 Pro Max 512GB 布根地紅色 监测到库存变化")
        self.assertIn("S1", body)
        self.assertIn("监测到库存变化", title)

    # ---- 2. edge A：新增 SKU 只推该 SKU 当前状态 ----
    def test_edge_a_new_sku_one_time(self):
        t = self._target("t1", ["P1"], ["S0"])
        self._run(self._cfg([t]))  # 首次全量
        _AVAIL[("P2", "S0")] = "available"  # P2 当前有货

        # 给目标新增 P2
        t2 = self._target("t1", ["P1", "P2"], ["S0"])
        self._run(self._cfg([t2]))

        # 本次应新增一条「P2 当前状态」推送（来自 t1 的 bark，且只含 P2）
        new = [c for c in self.captured if "当前库存如下" not in c[1]]
        # 上一步首次全量 1 条 + 这次新增 SKU 1 条
        self.assertEqual(len(self.captured), 2)
        p2_msg = self.captured[-1]
        self.assertIn("iPhone 18 Pro 256GB 黑色", p2_msg[1])  # P2 型号出现
        self.assertNotIn("iPhone 18 Pro Max", p2_msg[1])      # P1 未被重推

        # init 记录已包含 P2
        inited = state_mod.load_init(self.init_path)
        self.assertEqual(set(inited["t1"]["parts"]), {"P1", "P2"})

    # ---- 3. edge B：禁用→清除标记，重新启用→再次全量 ----
    def test_edge_b_disable_then_enable_full(self):
        t = self._target("t1", ["P1"], ["S0"])
        self._run(self._cfg([t]))
        self.assertIn("t1", state_mod.load_init(self.init_path))

        # 禁用：应清除 init 标记（下一轮因 disabled 被跳过，但标记已清）
        self._run(self._cfg([self._target("t1", ["P1"], ["S0"], enabled=False)]))
        self.assertNotIn("t1", state_mod.load_init(self.init_path))

        # 重新启用：再次走首次全量推送
        before = len(self.captured)
        self._run(self._cfg([self._target("t1", ["P1"], ["S0"], enabled=True)]))
        self.assertIn("t1", state_mod.load_init(self.init_path))
        self.assertTrue(any("当前库存如下" in c[1] for c in self.captured[before:]))

    # ---- 4. 必填守卫：parts 或 stores 为空 → 跳过 ----
    def test_required_guard_skips_empty(self):
        empty_parts = self._target("t_empty_parts", [], ["S0"])
        empty_stores = self._target("t_empty_stores", ["P1"], [])
        valid = self._target("t_valid", ["P1"], ["S0"])
        self._run(self._cfg([empty_parts, empty_stores, valid]))

        urls = {c[0] for c in self.captured}
        self.assertIn("https://bark/t_valid", urls)
        self.assertNotIn("https://bark/t_empty_parts", urls)
        self.assertNotIn("https://bark/t_empty_stores", urls)

    # ---- 5b. 首轮空快照护栏：未匹配到任何 SKU 不推送、不写 init，下轮重试 ----
    def test_first_full_empty_snapshot_no_push_no_init(self):
        # 让本轮 snapshot 里完全没有目标关注的 P1（模拟苹果接口偶发空响应）
        t = self._target("t1", ["P1"], ["S0"])
        snap = _make_snapshot(["P99"])  # P99 不在目标 parts 内
        with mock.patch.object(FakeSession, "fetch_all", return_value=(snap, [])):
            self._run(self._cfg([t]))
        # 不应有任何推送，且 init 标记不得写入（否则目标被锁死）
        self.assertEqual(len(self.captured), 0)
        self.assertNotIn("t1", state_mod.load_init(self.init_path))

        # 下一轮恢复正常数据 → 应触发首次全量推送并落标记
        self._run(self._cfg([t]))
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0][1], "正在为您监测 iPhone 库存，当前库存如下")
        self.assertIn("t1", state_mod.load_init(self.init_path))

    # ---- 5. 多型号按型号分条 ----
    def test_multi_model_split(self):
        _AVAIL[("P1", "S0")] = "available"
        _AVAIL[("P2", "S0")] = "available"
        # 同时关注两型号、都看 S0，且两种变动都推送
        t = self._target("t1", ["P1", "P2"], ["S0"], notify_on=["available", "unavailable"])
        self._run(self._cfg([t]))  # 首次全量（单条「首次快照」）
        # 两型号在 S0 同时售完（available -> unavailable）
        _AVAIL[("P1", "S0")] = "unavailable"
        _AVAIL[("P2", "S0")] = "unavailable"
        self._run(self._cfg([t]))
        # 变动推送应为 2 条（两个型号各一条）
        changes = [c for c in self.captured if "当前库存如下" not in c[1]]
        self.assertEqual(len(changes), 2)
        titles = {c[1] for c in changes}
        self.assertIn("您关注的 iPhone 18 Pro Max 512GB 布根地紅色 监测到库存变化", titles)
        self.assertIn("您关注的 iPhone 18 Pro 256GB 黑色 监测到库存变化", titles)


if __name__ == "__main__":
    unittest.main()
