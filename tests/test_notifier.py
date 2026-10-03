#!/usr/bin/env python3
"""notifier.py 单元测试（标准库 unittest）。"""

import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifier

SKU_META = {
    "MJXV4ZA/A": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
}

TARGET = {
    "id": "t1",
    "name": "我的手机",
    "bark_url": "https://api.day.app/KEY",
    "enabled": True,
    "parts": ["MJXV4ZA/A"],
    "stores": ["ifc mall", "Canton Road", "apm Hong Kong"],
    "notify_on": ["available"],
}


def fake_response(payload=b'{"code":200}'):
    """构造一个支持 with 的假响应对象。"""
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = payload
    return resp


def sent_payload(urlopen_mock):
    """从被打的 urlopen 桩里取出实际发出的 Request 与解析后的 payload。"""
    req = urlopen_mock.call_args[0][0]
    return req, json.loads(req.data.decode("utf-8"))


class SendTest(unittest.TestCase):
    def test_payload_fields_and_url_used_as_is(self):
        url = "https://bark.example.com/ABC/自托管路径"
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            ok, detail = notifier.send(url, "标题", "正文")
        self.assertTrue(ok)
        req, payload = sent_payload(m)
        self.assertEqual(req.full_url, url)  # URL 原样使用
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["body"], "正文")
        self.assertEqual(payload["group"], "AstoreWatcher")
        self.assertNotIn("icon", payload)
        self.assertNotIn("level", payload)
        self.assertNotIn("isArchive", payload)
        self.assertIn("200", detail)
        self.assertEqual(m.call_args[1]["timeout"], notifier.BARK_TIMEOUT)

    def test_optional_fields_included(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.send("https://x", "t", "b", icon="https://i", level="timeSensitive", is_archive=True)
        _, payload = sent_payload(m)
        self.assertEqual(payload["icon"], "https://i")
        self.assertEqual(payload["level"], "timeSensitive")
        self.assertEqual(payload["isArchive"], 1)

    def test_url_included_when_given(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.send("https://x", "t", "b", url="https://example.com/order")
        _, payload = sent_payload(m)
        self.assertEqual(payload["url"], "https://example.com/order")

    def test_url_omitted_when_empty(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.send("https://x", "t", "b", url="")
        _, payload = sent_payload(m)
        self.assertNotIn("url", payload)

    def test_is_archive_false_still_sent(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.send("https://x", "t", "b", is_archive=False)
        _, payload = sent_payload(m)
        self.assertEqual(payload["isArchive"], 0)

    def test_group_override(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.send("https://x", "t", "b", group="hk-watcher")
        _, payload = sent_payload(m)
        self.assertEqual(payload["group"], "hk-watcher")

    def test_http_error_captured(self):
        err = urllib.error.HTTPError("https://x", 500, "boom", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            ok, detail = notifier.send("https://x", "t", "b")
        self.assertFalse(ok)
        self.assertIn("500", detail)

    def test_url_error_captured(self):
        err = urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            ok, detail = notifier.send("https://x", "t", "b")
        self.assertFalse(ok)
        self.assertIn("URLError", detail)

    def test_empty_url_does_not_raise(self):
        ok, detail = notifier.send("", "t", "b")
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_unexpected_exception_captured(self):
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("kaboom")):
            ok, detail = notifier.send("https://x", "t", "b")
        self.assertFalse(ok)
        self.assertIn("kaboom", detail)


class FormatChangeTest(unittest.TestCase):
    def test_available_title(self):
        change = {"part": "MJXV4ZA/A", "store": "ifc mall",
                  "from": "unavailable", "to": "available", "quote": "備妥於： 今日"}
        title, body = notifier.format_change(change, SKU_META)
        self.assertEqual(title, "您关注的 iPhone 18 Pro Max 512GB 布根地紅色 监测到库存变化")
        self.assertEqual(body, "門店：Apple 中環\n狀態：備妥於： 今日")

    def test_unavailable_title(self):
        # 变动推送标题统一，不再区分有货/无货
        change = {"part": "MJXV4ZA/A", "store": "Canton Road",
                  "from": "available", "to": "unavailable", "quote": "暫無供應"}
        title, body = notifier.format_change(change, SKU_META)
        self.assertEqual(title, "您关注的 iPhone 18 Pro Max 512GB 布根地紅色 监测到库存变化")
        self.assertEqual(body, "門店：Apple 廣東道\n狀態：暫無供應")

    def test_missing_quote_fallback(self):
        change = {"part": "MJXV4ZA/A", "store": "apm Hong Kong", "to": "available", "quote": ""}
        _, body = notifier.format_change(change, SKU_META)
        self.assertEqual(body, "門店：Apple 觀塘\n狀態：可取貨")

    def test_missing_meta_falls_back_to_part(self):
        change = {"part": "UNKNOWN1", "store": "ifc mall", "to": "available", "quote": "q"}
        title, _ = notifier.format_change(change, SKU_META)
        self.assertEqual(title, "您关注的 UNKNOWN1 监测到库存变化")

    def test_empty_meta_dict(self):
        change = {"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "q"}
        title, _ = notifier.format_change(change, {})
        self.assertEqual(title, "您关注的 MJXV4ZA/A 监测到库存变化")


class PushTargetTest(unittest.TestCase):
    def test_disabled_target_returns_empty(self):
        target = dict(TARGET, enabled=False)
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "q"}]
        with mock.patch("urllib.request.urlopen") as m:
            self.assertEqual(notifier.push_target(target, changes, SKU_META), [])
        m.assert_not_called()

    def test_filters_changes_outside_target_parts(self):
        target = dict(TARGET, parts=["MJXV4ZA/A"])
        changes = [
            {"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "q1"},
            {"part": "OTHERPART", "store": "ifc mall", "to": "available", "quote": "q2"},
        ]
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            results = notifier.push_target(target, changes, SKU_META)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][0])
        _, payload = sent_payload(m)
        self.assertNotIn("OTHERPART", payload["body"])

    def test_empty_parts_is_skipped(self):
        # SKU 与门店均为必填：空 parts / 空 stores 不再当作「全部」，直接跳过
        target = dict(TARGET, parts=[])
        changes = [{"part": "OTHERPART", "store": "ifc mall", "to": "available", "quote": "q"}]
        with mock.patch("urllib.request.urlopen") as m:
            self.assertEqual(notifier.push_target(target, changes, SKU_META), [])
        m.assert_not_called()

    def test_empty_stores_is_skipped(self):
        target = dict(TARGET, stores=[])
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "q"}]
        with mock.patch("urllib.request.urlopen") as m:
            self.assertEqual(notifier.push_target(target, changes, SKU_META), [])
        m.assert_not_called()

    def test_no_changes_returns_empty(self):
        with mock.patch("urllib.request.urlopen") as m:
            self.assertEqual(notifier.push_target(TARGET, [], SKU_META), [])
        m.assert_not_called()

    def test_same_sku_many_stores_merged_not_truncated(self):
        """同一 SKU 的多家门店合并进一行，不再按条数截断 —— 6 家门店也列得下。"""
        store_names = [f"store{i}" for i in range(notifier.MAX_LISTED + 2)]
        # push_target 只推目标勾选的门店，所以这里要把这些虚拟门店一并勾上
        target = dict(TARGET, parts=["MJXV4ZA/A"], stores=store_names)
        changes = [{"part": "MJXV4ZA/A", "store": s, "to": "available", "quote": f"q{i}"}
                   for i, s in enumerate(store_names)]
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            results = notifier.push_target(target, changes, SKU_META)
        self.assertEqual(len(results), 1)  # 合并成一条推送
        self.assertTrue(results[0][0])
        _, payload = sent_payload(m)
        body = payload["body"]
        self.assertNotIn("等", body)
        for i in range(notifier.MAX_LISTED + 2):
            self.assertIn(f"store{i}", body)
        # 型号名只出现一次 —— 这是「合并了」的直接证据
        self.assertEqual(body.count("iPhone 18 Pro Max 512GB 布根地紅色"), 1)

    def test_single_change_has_no_ellipsis(self):
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "q"}]
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.push_target(TARGET, changes, SKU_META)
        _, payload = sent_payload(m)
        self.assertNotIn("等", payload["body"])

    def test_send_failure_propagates_as_false(self):
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "q"}]
        with mock.patch("urllib.request.urlopen", side_effect=OSError("network down")):
            results = notifier.push_target(TARGET, changes, SKU_META)
        self.assertEqual(results[0][0], False)
        self.assertIn("network down", results[0][1])


class BuildMessagesTest(unittest.TestCase):
    """push_mode：merged（默认）vs per_store。"""

    def _changes(self):
        return [
            {"part": "MJXV4ZA/A", "store": "ifc mall",
             "from": "unavailable", "to": "available", "quote": "備妥於： 今日"},
            {"part": "MJXV4ZA/A", "store": "Canton Road",
             "from": "available", "to": "unavailable", "quote": "暫無供應"},
        ]

    def test_default_is_merged_single_message(self):
        msgs = notifier.build_messages({"push_mode": "merged"}, self._changes(), SKU_META)
        self.assertEqual(len(msgs), 1)
        title, body, url = msgs[0]
        # 单 SKU 变动 → 标题用完整型号（型号+颜色+容量）
        self.assertEqual(title, "您关注的 iPhone 18 Pro Max 512GB 布根地紅色 监测到库存变化")
        self.assertIn("Apple 中環", body)
        self.assertIn("Apple 廣東道", body)
        # 含「有货」变动 → 回退 Apple 主页
        self.assertEqual(url, notifier.APPLE_HOME)

    def test_merged_splits_by_model(self):
        meta = {
            "P1": {"model": "iPhone 18 Pro", "color": "黑色", "capacity": "256GB"},
            "P2": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
        }
        changes = [
            {"part": "P1", "store": "ifc mall", "to": "available", "quote": "q1"},
            {"part": "P2", "store": "Canton Road", "to": "available", "quote": "q2"},
        ]
        msgs = notifier.build_messages({"push_mode": "merged"}, changes, meta)
        self.assertEqual(len(msgs), 2)  # 两个型号 → 两条推送
        titles = sorted(t for t, _, _ in msgs)
        # 单 SKU 变动的型号用完整型号标题；另一个型号同理
        self.assertEqual(titles, sorted([
            "您关注的 iPhone 18 Pro 256GB 黑色 监测到库存变化",
            "您关注的 iPhone 18 Pro Max 512GB 布根地紅色 监测到库存变化",
        ]))
        self.assertIn("黑色", msgs[0][1] + msgs[1][1])

    def test_merged_multi_sku_same_model_uses_model_title(self):
        meta = {
            "P1": {"model": "iPhone 18 Pro Max", "color": "黑色", "capacity": "256GB"},
            "P2": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
        }
        changes = [
            {"part": "P1", "store": "ifc mall", "to": "available", "quote": "q1"},
            {"part": "P2", "store": "Canton Road", "to": "available", "quote": "q2"},
        ]
        msgs = notifier.build_messages({"push_mode": "merged"}, changes, meta)
        self.assertEqual(len(msgs), 1)  # 同型号多条 → 合并一条，标题用型号名
        self.assertEqual(msgs[0][0], "您关注的 iPhone 18 Pro Max 监测到库存变化")
        self.assertIn("黑色", msgs[0][1])
        self.assertIn("布根地紅色", msgs[0][1])

    def test_missing_push_mode_falls_back_to_merged(self):
        msgs = notifier.build_messages({}, self._changes(), SKU_META)
        self.assertEqual(len(msgs), 1)

    def test_invalid_push_mode_falls_back_to_merged(self):
        msgs = notifier.build_messages({"push_mode": "wat"}, self._changes(), SKU_META)
        self.assertEqual(len(msgs), 1)

    def test_per_store_splits_by_store(self):
        msgs = notifier.build_messages({"push_mode": "per_store"}, self._changes(), SKU_META)
        self.assertEqual(len(msgs), 2)
        titles = [t for t, _, _ in msgs]
        self.assertEqual(titles[0], "Apple 中環 有貨了")
        self.assertEqual(titles[1], "Apple 廣東道 無貨了")
        self.assertIn("iPhone 18 Pro Max 512GB 布根地紅色：備妥於： 今日", msgs[0][1])
        self.assertIn("暫無供應", msgs[1][1])
        # 每条只含自己门店的内容
        self.assertNotIn("Apple 廣東道", msgs[0][1])
        self.assertNotIn("Apple 中環", msgs[1][1])
        # 有货的门店才带 url，无货的不带
        self.assertEqual(msgs[0][2], notifier.APPLE_HOME)
        self.assertIsNone(msgs[1][2])

    def test_url_uses_target_order_url_when_available(self):
        target = {"push_mode": "merged", "order_url": "https://buy.example.com/iphone"}
        msgs = notifier.build_messages(target, self._changes(), SKU_META)
        self.assertEqual(msgs[0][2], "https://buy.example.com/iphone")

    def test_url_is_none_when_no_available(self):
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall",
                    "from": "available", "to": "unavailable", "quote": "暫無供應"}]
        target = {"push_mode": "merged", "order_url": "https://buy.example.com/iphone"}
        msgs = notifier.build_messages(target, changes, SKU_META)
        self.assertIsNone(msgs[0][2])

    def test_merged_truncation_applies_per_sku(self):
        """截断口径是 SKU 数（不是门店数）：超过 MAX_LISTED 个 SKU 才补「等 N 條」。"""
        meta = {f"P{i}": {"model": "iPhone 18 Pro", "color": "黑色", "capacity": f"{i}GB"}
                for i in range(notifier.MAX_LISTED + 1)}
        changes = [{"part": f"P{i}", "store": "ifc mall", "to": "available", "quote": "q"}
                   for i in range(notifier.MAX_LISTED + 1)]
        title, body, _ = notifier.build_messages({"push_mode": "merged"}, changes, meta)[0]
        self.assertIn("等 1 條", body)
        self.assertTrue(title)

    def test_merged_same_sku_multi_store_one_line(self):
        """同一 SKU 多家门店补货 → 一行「可取貨：A、B」，不再每家店重复一遍型号名；
        且每家店要带上自己的 quote（如「備妥於： 今日」），否则看不出什么时候能取。"""
        changes = [
            {"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available", "quote": "今日可取"},
            {"part": "MJXV4ZA/A", "store": "Causeway Bay", "to": "available", "quote": "明日可取"},
        ]
        _, body, _ = notifier.build_messages({"push_mode": "merged"}, changes, SKU_META)[0]
        self.assertEqual(body.split("\n")[1],
                         "可取貨：Apple 中環（今日可取）、Apple 銅鑼灣（明日可取）")

    def test_merged_skips_quote_when_absent(self):
        """没有 quote 时不能拼出空的括号，只显示店名。"""
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall", "to": "available"}]
        _, body, _ = notifier.build_messages({"push_mode": "merged"}, changes, SKU_META)[0]
        self.assertEqual(body.split("\n")[1], "可取貨：Apple 中環")

    def test_merged_availability_split_into_two_lines(self):
        """一店补货、一店售完 → 拆成「可取貨：」与「暫無供應：」两行。"""
        _, body, url = notifier.build_messages({"push_mode": "merged"},
                                               self._changes(), SKU_META)[0]
        self.assertIn("可取貨：Apple 中環", body)
        self.assertIn("暫無供應：Apple 廣東道", body)
        self.assertEqual(url, notifier.APPLE_HOME)

    def test_merged_multiple_skus_each_get_a_block(self):
        """同型号下多个 SKU → 各占一段，标题退化为型号名。"""
        meta = {
            "P1": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "256GB"},
            "P2": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
        }
        changes = [{"part": "P1", "store": "ifc mall", "to": "available", "quote": "q"},
                   {"part": "P2", "store": "ifc mall", "to": "available", "quote": "q"}]
        msgs = notifier.build_messages({"push_mode": "merged"}, changes, meta)
        self.assertEqual(len(msgs), 1)
        title, body, _ = msgs[0]
        self.assertEqual(title, "您关注的 iPhone 18 Pro Max 监测到库存变化")
        self.assertIn("iPhone 18 Pro Max 256GB 布根地紅色", body)
        self.assertIn("iPhone 18 Pro Max 512GB 布根地紅色", body)

    def test_per_store_splits_available_and_unavailable(self):
        """同一家店同时有补货和售罄 → 拆成两条，每条只含同一方向的 SKU。
        混在一条「有貨了」里夹着「暫無供應」会让人不知道到底能不能买。"""
        meta = {
            "P1": {"model": "iPhone 18 Pro", "color": "黑色", "capacity": "256GB"},
            "P2": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
        }
        changes = [
            {"part": "P1", "store": "ifc mall", "to": "available", "quote": "今日可取"},
            {"part": "P2", "store": "ifc mall", "to": "unavailable", "quote": "暫無供應"},
        ]
        msgs = notifier.build_messages({"push_mode": "per_store"}, changes, meta)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0][0], "Apple 中環 有貨了")
        self.assertEqual(msgs[0][1].split("\n")[0], "iPhone 18 Pro 256GB 黑色：今日可取")
        self.assertEqual(msgs[0][2], notifier.APPLE_HOME)   # 有货才带链接
        self.assertEqual(msgs[1][0], "Apple 中環 無貨了")
        self.assertEqual(msgs[1][1].split("\n")[0], "iPhone 18 Pro Max 512GB 布根地紅色：暫無供應")
        self.assertIsNone(msgs[1][2])

    def test_per_store_grouping_same_store(self):
        meta = {
            "P1": {"model": "iPhone 18 Pro", "color": "黑色", "capacity": "256GB"},
            "P2": {"model": "iPhone 18 Pro Max", "color": "布根地紅色", "capacity": "512GB"},
        }
        changes = [
            {"part": "P1", "store": "ifc mall", "to": "available", "quote": "q1"},
            {"part": "P2", "store": "ifc mall", "to": "available", "quote": "q2"},
        ]
        msgs = notifier.build_messages({"push_mode": "per_store"}, changes, meta)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][0], "Apple 中環 有貨了")
        self.assertEqual(len(msgs[0][1].split("\n")), 2)

    def test_push_target_per_store_sends_one_per_store(self):
        target = dict(TARGET, push_mode="per_store")
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            results = notifier.push_target(target, self._changes(), SKU_META)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(ok for ok, _ in results))
        self.assertEqual(m.call_count, 2)

    def test_push_target_merged_sends_one(self):
        target = dict(TARGET, push_mode="merged")
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            results = notifier.push_target(target, self._changes(), SKU_META)
        self.assertEqual(len(results), 1)
        self.assertEqual(m.call_count, 1)

    def test_push_target_attaches_apple_icon_always(self):
        # 所有推送都带 Apple 图标
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.push_target(TARGET, self._changes(), SKU_META)
        _, payload = sent_payload(m)
        self.assertEqual(payload["icon"], notifier.APPLE_ICON)

    def test_push_target_url_only_when_available(self):
        # 含「有货」变动 → 带 url（目标未填 order_url 则回退主页）
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.push_target(TARGET, self._changes(), SKU_META)
        _, payload = sent_payload(m)
        self.assertEqual(payload["url"], notifier.APPLE_HOME)

    def test_push_target_url_uses_target_order_url(self):
        target = dict(TARGET, order_url="https://buy.example.com/x")
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.push_target(target, self._changes(), SKU_META)
        _, payload = sent_payload(m)
        self.assertEqual(payload["url"], "https://buy.example.com/x")

    def test_push_target_no_url_when_only_unavailable(self):
        changes = [{"part": "MJXV4ZA/A", "store": "ifc mall",
                    "from": "available", "to": "unavailable", "quote": "暫無供應"}]
        with mock.patch("urllib.request.urlopen", return_value=fake_response()) as m:
            notifier.push_target(TARGET, changes, SKU_META)
        _, payload = sent_payload(m)
        self.assertNotIn("url", payload)


class FormatSnapshotTest(unittest.TestCase):
    STORES = ["ifc mall", "Canton Road", "Causeway Bay",
              "Festival Walk", "apm Hong Kong", "New Town Plaza"]

    def _snapshot(self, available_stores):
        return {"MJXV4ZA/A": {s: {"display": "available" if s in available_stores else "unavailable",
                                  "quote": "備妥於： 今日" if s in available_stores else "暫無供應"}
                              for s in self.STORES}}

    def test_title_counts_skus(self):
        snapshot = {"MJXV4ZA/A": self._snapshot([])["MJXV4ZA/A"],
                    "MJXQ4ZA/A": self._snapshot([])["MJXV4ZA/A"]}
        title, _ = notifier.format_snapshot(snapshot, None, SKU_META)
        self.assertEqual(title, "正在为您监测库存，当前库存如下")

    def test_available_lists_store_names(self):
        title, body = notifier.format_snapshot(self._snapshot(["ifc mall", "Canton Road"]),
                                               ["MJXV4ZA/A"], SKU_META)
        self.assertEqual(
            body,
            "iPhone 18 Pro Max 512GB 布根地紅色：2 店可取貨：Apple 中環、Apple 廣東道",
        )

    def test_all_unavailable_line(self):
        _, body = notifier.format_snapshot(self._snapshot([]), ["MJXV4ZA/A"], SKU_META)
        self.assertIn("6 店暫無供應", body)

    def test_all_available_lists_every_store(self):
        """有货时必须给出能去哪买的店名 —— 「N 店可取货」这句话没法拿去 actionable。"""
        _, body = notifier.format_snapshot(self._snapshot(self.STORES), ["MJXV4ZA/A"], SKU_META)
        self.assertIn("6 店可取貨", body)
        for zh in ("Apple 中環", "Apple 廣東道", "Apple 銅鑼灣",
                   "Apple 九龍塘", "Apple 觀塘", "Apple 沙田"):
            self.assertIn(zh, body)

    def test_parts_filter_and_order(self):
        snapshot = {"MJXV4ZA/A": self._snapshot([])["MJXV4ZA/A"],
                    "MJXQ4ZA/A": self._snapshot(["ifc mall"])["MJXV4ZA/A"]}
        title, body = notifier.format_snapshot(snapshot, ["MJXQ4ZA/A"], SKU_META)
        self.assertEqual(title, "正在为您监测库存，当前库存如下")
        self.assertEqual(len(body.split("\n")), 1)
        self.assertIn("1 店可取貨：Apple 中環", body)

    def test_part_without_data(self):
        # part 在关注列表里但本轮没查到门店数据
        _, body = notifier.format_snapshot({"MJXV4ZA/A": {}}, ["MJXV4ZA/A"], SKU_META)
        self.assertIn("未取到數據", body)

    def test_part_without_data_survives_store_filter(self):
        """勾选门店后（门店已是必填，生产上永远走这条路径）也不能让 SKU 整行消失：
        否则「本轮没查到」和「全无货」在推送上表现一样，用户无从分辨。"""
        _, body = notifier.format_snapshot({"MJXV4ZA/A": {}}, ["MJXV4ZA/A"], SKU_META,
                                           ["Apple 中環"])
        self.assertIn("未取到數據", body)

    def test_part_missing_from_snapshot_omitted(self):
        # 与 diff / initial_snapshot 一致：不在 snapshot 里的 part 直接跳过
        _, body = notifier.format_snapshot({}, ["MJXV4ZA/A"], SKU_META)
        self.assertEqual(body, "")

    def test_empty_snapshot(self):
        title, body = notifier.format_snapshot({}, None, SKU_META)
        self.assertEqual(title, "正在为您监测库存，当前库存如下")
        self.assertEqual(body, "")

    def test_bad_input_does_not_raise(self):
        title, body = notifier.format_snapshot(None, None, None)
        self.assertEqual(title, "正在为您监测库存，当前库存如下")
        self.assertEqual(body, "")


if __name__ == "__main__":
    unittest.main()
