#!/usr/bin/env python3
"""config.py 单元测试（标准库 unittest）。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


class DefaultConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = config.default_config()
        self.assertEqual(cfg["location"], "中環")
        self.assertEqual(cfg["interval_sec"], 120)
        self.assertNotIn("jitter_sec", cfg)
        self.assertEqual(cfg["targets"], [])

    def test_default_is_fresh_copy(self):
        a = config.default_config()
        a["targets"].append({"id": "x"})
        self.assertEqual(config.default_config()["targets"], [])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_default(self):
        cfg = config.load(os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(cfg, config.default_config())

    def test_corrupt_json_backed_up_as_bad(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json ]")
        cfg = config.load(self.path)
        self.assertEqual(cfg, config.default_config())
        self.assertTrue(os.path.exists(self.path + ".bad"))
        self.assertFalse(os.path.exists(self.path))

    def test_normal_file_validated(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"interval_sec": 5, "targets": [{"bark_url": "https://x"}]}, f)
        cfg = config.load(self.path)
        self.assertEqual(cfg["interval_sec"], 30)  # 被夹到下限
        self.assertEqual(cfg["location"], "中環")  # 缺失字段补默认
        self.assertEqual(len(cfg["targets"]), 1)

    def test_non_dict_json_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(config.load(self.path), config.default_config())


class ValidateTest(unittest.TestCase):
    def test_interval_clamped_to_minimum(self):
        self.assertEqual(config.validate({"interval_sec": 1})["interval_sec"], 30)
        self.assertEqual(config.validate({"interval_sec": 29})["interval_sec"], 30)
        self.assertEqual(config.validate({"interval_sec": 300})["interval_sec"], 300)

    def test_interval_invalid_falls_back(self):
        self.assertEqual(config.validate({"interval_sec": "abc"})["interval_sec"], 120)
        self.assertEqual(config.validate({})["interval_sec"], 120)

    def test_jitter_sec_dropped(self):
        # 抖动改为代码内固定随机值，不再作为配置项
        self.assertNotIn("jitter_sec", config.validate({"jitter_sec": 45}))

    def test_stores_preserved_and_deduped(self):
        cfg = config.validate({"targets": [{
            "bark_url": "https://api.day.app/KEY",
            "stores": ["Apple 中環", "Apple 中環", 123, "Apple 銅鑼灣"],
        }]})
        self.assertEqual(cfg["targets"][0]["stores"], ["Apple 中環", "Apple 銅鑼灣"])

    def test_empty_bark_url_target_dropped(self):
        cfg = config.validate({"targets": [
            {"id": "a", "bark_url": ""},
            {"id": "b", "bark_url": "   "},
            {"id": "c", "bark_url": "https://api.day.app/KEY"},
            {"id": "d"},
        ]})
        self.assertEqual([t["id"] for t in cfg["targets"]], ["c"])

    def test_parts_filtered_by_valid_parts(self):
        cfg = config.validate(
            {"targets": [{"id": "t", "bark_url": "u", "parts": ["P1", "BAD", "P2"]}]},
            valid_parts=["P1", "P2"],
        )
        self.assertEqual(cfg["targets"][0]["parts"], ["P1", "P2"])

    def test_parts_not_filtered_when_valid_parts_empty(self):
        cfg = config.validate({"targets": [{"bark_url": "u", "parts": ["P1", "BAD"]}]})
        self.assertEqual(cfg["targets"][0]["parts"], ["P1", "BAD"])

    def test_notify_on_normalized(self):
        cfg = config.validate({"targets": [
            {"bark_url": "u1", "notify_on": ["unavailable", "available", "bogus"]},
            {"bark_url": "u2", "notify_on": []},
            {"bark_url": "u3", "notify_on": "unavailable"},
        ]})
        self.assertEqual(cfg["targets"][0]["notify_on"], ["unavailable", "available"])
        self.assertEqual(cfg["targets"][1]["notify_on"], ["available"])
        self.assertEqual(cfg["targets"][2]["notify_on"], ["unavailable"])

    def test_ids_generated_and_unique(self):
        cfg = config.validate({"targets": [
            {"bark_url": "u1"},
            {"bark_url": "u2", "id": "  "},
            {"bark_url": "u3", "id": "dup"},
            {"bark_url": "u4", "id": "dup"},
        ]})
        ids = [t["id"] for t in cfg["targets"]]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(i for i in ids))

    def test_push_mode_normalized(self):
        cfg = config.validate({"targets": [
            {"bark_url": "u1"},
            {"bark_url": "u2", "push_mode": "per_store"},
            {"bark_url": "u3", "push_mode": " per_store "},
            {"bark_url": "u4", "push_mode": "bogus"},
        ]})
        modes = [t["push_mode"] for t in cfg["targets"]]
        self.assertEqual(modes, ["merged", "per_store", "per_store", "merged"])
        self.assertTrue(all(m in config.VALID_PUSH_MODES for m in modes))

    def test_non_dict_input(self):
        self.assertEqual(config.validate(None), config.default_config())
        self.assertEqual(config.validate("junk")["targets"], [])


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_creates_dir_and_valid_json(self):
        config.save(self.path, config.default_config())
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertEqual(json.loads(raw), config.default_config())

    def test_roundtrip(self):
        cfg = config.validate({"location": "沙田", "interval_sec": 90,
                               "targets": [{"id": "t1", "name": "我的手机",
                                            "bark_url": "https://api.day.app/KEY",
                                            "enabled": False,
                                            "parts": ["MJXV4ZA/A"],
                                            "notify_on": ["available", "unavailable"]}]})
        config.save(self.path, cfg)
        self.assertEqual(config.load(self.path), cfg)

    def test_no_temp_files_left_behind(self):
        config.save(self.path, config.default_config())
        leftovers = [n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class LoadCachedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        config.save(self.path, config.default_config())

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_mtime_returns_cached_object(self):
        cache = {"path": None, "mtime": None, "data": None}
        first = config.load_cached(self.path, cache)
        second = config.load_cached(self.path, cache)
        self.assertIs(first, second)

    def test_changed_mtime_reloads(self):
        cache = {"path": None, "mtime": None, "data": None}
        first = config.load_cached(self.path, cache)
        new_cfg = dict(config.default_config())
        new_cfg["interval_sec"] = 600
        config.save(self.path, new_cfg)
        os.utime(self.path, (1000000, 1000000))  # 确保 mtime 一定变化
        second = config.load_cached(self.path, cache)
        self.assertEqual(second["interval_sec"], 600)
        self.assertIsNot(first, second)

    def test_missing_file_cached(self):
        cache = {"path": None, "mtime": None, "data": None}
        missing = os.path.join(self.tmp.name, "nope.json")
        self.assertEqual(config.load_cached(missing, cache), config.default_config())
        self.assertEqual(cache["mtime"], None)


if __name__ == "__main__":
    unittest.main()
