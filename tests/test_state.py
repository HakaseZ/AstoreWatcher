#!/usr/bin/env python3
"""state.py 单元测试（标准库 unittest）。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state


def snap(part, store, display, quote="備妥於： 今日"):
    return {part: {store: {"display": display, "quote": quote}}}


class DiffTest(unittest.TestCase):
    def test_first_run_only_reports_available(self):
        # 首轮：有货要报
        changes = state.diff({}, snap("P1", "ifc mall", "available"), None, ["available"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], None)
        self.assertEqual(changes[0]["to"], "available")
        self.assertEqual(changes[0]["quote"], "備妥於： 今日")
        # 首轮：无货不报（否则一启动就刷屏）
        self.assertEqual(state.diff({}, snap("P1", "ifc mall", "unavailable"),
                                    None, ["available", "unavailable"]), [])

    def test_available_to_unavailable_reported(self):
        prev = {"P1": {"ifc mall": "available"}}
        changes = state.diff(prev, snap("P1", "ifc mall", "unavailable", "暫無供應"),
                             ["P1"], ["available", "unavailable"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "available")
        self.assertEqual(changes[0]["to"], "unavailable")

    def test_unavailable_to_available_reported(self):
        prev = {"P1": {"ifc mall": "unavailable"}}
        changes = state.diff(prev, snap("P1", "ifc mall", "available"), ["P1"], ["available"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["from"], "unavailable")

    def test_no_change_no_alert(self):
        prev = {"P1": {"ifc mall": "available"}}
        self.assertEqual(state.diff(prev, snap("P1", "ifc mall", "available"),
                                    ["P1"], ["available", "unavailable"]), [])
        prev = {"P1": {"ifc mall": "unavailable"}}
        self.assertEqual(state.diff(prev, snap("P1", "ifc mall", "unavailable"),
                                    ["P1"], ["available", "unavailable"]), [])

    def test_notify_on_available_only_ignores_unavailable(self):
        prev = {"P1": {"ifc mall": "available"}}
        self.assertEqual(state.diff(prev, snap("P1", "ifc mall", "unavailable"),
                                    ["P1"], ["available"]), [])

    def test_notify_on_unavailable_only(self):
        prev = {"P1": {"ifc mall": "available"}}
        changes = state.diff(prev, snap("P1", "ifc mall", "unavailable"), ["P1"], ["unavailable"])
        self.assertEqual(len(changes), 1)
        # 该 notify_on 下上货不报
        self.assertEqual(state.diff({"P1": {"ifc mall": "unavailable"}},
                                    snap("P1", "ifc mall", "available"), ["P1"], ["unavailable"]), [])

    def test_parts_filter(self):
        snapshot = {
            "P1": {"ifc mall": {"display": "available", "quote": "q1"}},
            "P2": {"ifc mall": {"display": "available", "quote": "q2"}},
        }
        changes = state.diff({}, snapshot, ["P2"], ["available"])
        self.assertEqual([c["part"] for c in changes], ["P2"])

    def test_empty_parts_means_all(self):
        snapshot = {
            "P1": {"ifc mall": {"display": "available", "quote": "q1"}},
            "P2": {"ifc mall": {"display": "available", "quote": "q2"}},
        }
        self.assertEqual(len(state.diff({}, snapshot, None, ["available"])), 2)

    def test_part_missing_in_snapshot_skipped(self):
        self.assertEqual(state.diff({}, {}, ["P1"], ["available"]), [])

    def test_bad_input_does_not_raise(self):
        self.assertEqual(state.diff(None, None, None, None), [])
        self.assertEqual(state.diff("junk", "junk", ["P1"], "junk"), [])


class ApplyTest(unittest.TestCase):
    def test_merge_keeps_display_only(self):
        prev = {"P1": {"ifc mall": "available"}}
        snapshot = {
            "P1": {"ifc mall": {"display": "unavailable", "quote": "暫無供應"},
                   "Canton Road": {"display": "available", "quote": "今日"}},
            "P2": {"apm Hong Kong": {"display": "available", "quote": "今日"}},
        }
        merged = state.apply(prev, snapshot)
        self.assertEqual(merged["P1"]["ifc mall"], "unavailable")
        self.assertEqual(merged["P1"]["Canton Road"], "available")
        self.assertEqual(merged["P2"]["apm Hong Kong"], "available")
        self.assertNotIn("quote", merged["P1"]["ifc mall"])

    def test_does_not_mutate_prev(self):
        prev = {"P1": {"ifc mall": "available"}}
        state.apply(prev, snap("P1", "ifc mall", "unavailable"))
        self.assertEqual(prev, {"P1": {"ifc mall": "available"}})

    def test_keeps_old_parts_absent_from_snapshot(self):
        merged = state.apply({"P9": {"store": "available"}}, {"P1": {"store": {"display": "unavailable"}}})
        self.assertEqual(merged["P9"]["store"], "available")


class InitialSnapshotTest(unittest.TestCase):
    def test_includes_unavailable(self):
        snapshot = {
            "P1": {"ifc mall": {"display": "available", "quote": "備妥於： 今日"},
                   "Canton Road": {"display": "unavailable", "quote": "暫無供應"}},
            "P2": {"ifc mall": {"display": "unavailable", "quote": "暫無供應"}},
        }
        entries = state.initial_snapshot(snapshot, ["P1", "P2"])
        self.assertEqual(len(entries), 3)  # 含 unavailable
        self.assertTrue(all(e["from"] is None for e in entries))
        self.assertEqual([e["to"] for e in entries],
                         ["available", "unavailable", "unavailable"])
        self.assertEqual(entries[0]["quote"], "備妥於： 今日")

    def test_parts_filter(self):
        snapshot = {
            "P1": {"ifc mall": {"display": "available", "quote": "q1"}},
            "P2": {"ifc mall": {"display": "unavailable", "quote": "q2"}},
        }
        entries = state.initial_snapshot(snapshot, ["P2"])
        self.assertEqual([e["part"] for e in entries], ["P2"])

    def test_empty_parts_means_all(self):
        snapshot = {"P1": {"s": {"display": "unavailable", "quote": "q"}},
                    "P2": {"s": {"display": "available", "quote": "q"}}}
        self.assertEqual(len(state.initial_snapshot(snapshot, None)), 2)

    def test_bad_input_returns_empty(self):
        self.assertEqual(state.initial_snapshot(None, ["P1"]), [])
        self.assertEqual(state.initial_snapshot("junk", None), [])

    def test_after_initial_snapshot_diff_still_dedupes(self):
        """全量推送写进 state 后，后续仍只在变化时报。"""
        snapshot = {"P1": {"ifc mall": {"display": "unavailable", "quote": "暫無供應"}}}
        new_state = state.apply({}, snapshot)
        self.assertEqual(state.diff(new_state, snapshot, ["P1"], ["available", "unavailable"]), [])
        # 之后补货 → 报警
        changed = {"P1": {"ifc mall": {"display": "available", "quote": "備妥於： 今日"}}}
        self.assertEqual(len(state.diff(new_state, changed, ["P1"], ["available"])), 1)


class InitFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "init.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_empty(self):
        self.assertEqual(state.load_init(os.path.join(self.tmp.name, "nope.json")), {})

    def test_load_corrupt_returns_empty_and_backup(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{{{ broken")
        self.assertEqual(state.load_init(self.path), {})
        self.assertTrue(os.path.exists(self.path + ".bad"))

    def test_save_and_load_roundtrip(self):
        mapping = {"t1": "2026-09-29T13:40:00", "t2": "2026-09-29T13:41:00"}
        state.save_init(self.path, mapping)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), mapping)
        self.assertEqual(state.load_init(self.path), mapping)

    def test_save_creates_dir(self):
        nested = os.path.join(self.tmp.name, "a", "b", "init.json")
        state.save_init(nested, {"t1": "now"})
        self.assertEqual(state.load_init(nested), {"t1": "now"})

    def test_non_dict_json_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2], f)
        self.assertEqual(state.load_init(self.path), {})

    def test_update_init_preserves_untouched_keys(self):
        # 并发修复的核心：update_init 必须在「最新」内容上应用增删，
        # 不能把其他进程（web）并发写入的标记覆盖掉。
        state.save_init(self.path, {
            "web": {"since": "2026-09-29T00:00:00", "parts": ["P1"]},
            "old": {"since": "2026-09-29T00:00:00", "parts": ["P2"]},
        })
        # 模拟 watcher 本轮只动了 watcher 目标：新增 w1、摘除 old
        state.update_init(self.path, add={"w1": {"since": "2026-09-29T01:00:00", "parts": ["P3"]}},
                          remove={"old"})
        result = state.load_init(self.path)
        # web 并发写入的标记必须被保留
        self.assertEqual(result.get("web"), {"since": "2026-09-29T00:00:00", "parts": ["P1"]})
        # watcher 自己的增删生效
        self.assertEqual(result.get("w1"), {"since": "2026-09-29T01:00:00", "parts": ["P3"]})
        self.assertNotIn("old", result)

    def test_update_init_noop_when_empty(self):
        state.save_init(self.path, {"keep": {"since": "x", "parts": []}})
        state.update_init(self.path)  # 不传 add/remove 不应改动文件
        self.assertEqual(state.load_init(self.path), {"keep": {"since": "x", "parts": []}})


class FileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_empty(self):
        self.assertEqual(state.load(os.path.join(self.tmp.name, "nope.json")), {})

    def test_load_corrupt_returns_empty_and_backup(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{{{ broken")
        self.assertEqual(state.load(self.path), {})
        self.assertTrue(os.path.exists(self.path + ".bad"))

    def test_save_and_load_roundtrip(self):
        data = {"MJXV4ZA/A": {"ifc mall": "available", "apm Hong Kong": "unavailable"}}
        state.save(self.path, data)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), data)
        self.assertEqual(state.load(self.path), data)

    def test_save_creates_dir(self):
        nested = os.path.join(self.tmp.name, "a", "b", "state.json")
        state.save(nested, {})
        self.assertTrue(os.path.exists(nested))


if __name__ == "__main__":
    unittest.main()
