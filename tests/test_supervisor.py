#!/usr/bin/env python3
"""supervisor + worker 的失败模式测试：不动真实浏览器，用假 worker 脚本替代。

这些用例的价值在于**复现已无法在本机靠真浏览器复现的故障**：浏览器挂死、SIGKILL、
profile 损坏、陈旧单例锁。它们都通过假 worker 子进程表达，因此 spawn / join /
killpg / 重试阶梯这些逻辑是被真实地跑一遍的，而不是被 mock 掉的。

刻意不碰本机的 data/ 目录 —— profile 里是真实的 shield cookie。
"""

import json
import os
import socket
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import supervisor
import worker


def write_fake_worker(tmp, body):
    path = os.path.join(tmp, "fake_worker.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))
    return path


# 各色假 worker ----

WORKER_OK = """
    import json, sys
    out = sys.argv[sys.argv.index("--out") + 1]
    json.dump({"ok": True, "checked_at": "2026-10-01 17:32",
               "snapshot": {"P1": {"S0": {"display": "available", "quote": "今日"}}},
               "failed": [], "kind": "OK", "error": ""}, open(out, "w"))
    sys.exit(0)
    """

WORKER_HANG = "import time\ntime.sleep(60)\n"


def worker_body(code, kind):
    return f"""
    import json, sys
    out = sys.argv[sys.argv.index("--out") + 1]
    json.dump({{"ok": False, "checked_at": None, "snapshot": {{}}, "failed": [],
               "kind": "{kind}", "error": "{kind} from fake worker"}}, open(out, "w"))
    sys.exit({code})
    """


WORKER_NO_OUT = "import sys\nsys.exit(9)\n"

WORKER_BAD_JSON = """
    import sys
    with open(sys.argv[sys.argv.index("--out") + 1], "w") as out:
        out.write("not json at all")
    sys.exit(0)
    """


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.profile = os.path.join(self.data, "profile")
        os.makedirs(self.profile, exist_ok=True)
        self._orig_worker = os.environ.get("ASTORE_WORKER")
        self.addCleanup(self._restore_worker)

    def _restore_worker(self):
        if self._orig_worker is None:
            os.environ.pop("ASTORE_WORKER", None)
        else:
            os.environ["ASTORE_WORKER"] = self._orig_worker

    def _use(self, body):
        os.environ["ASTORE_WORKER"] = write_fake_worker(self.data, body)

    def _run(self, timeout=5):
        return supervisor.run_round(self.profile, ["P1"], location="中環",
                                    data_dir=self.data, timeout=timeout)

    # ---- 正常路径 ----
    def test_success_returns_snapshot(self):
        self._use(WORKER_OK)
        r = self._run()
        self.assertTrue(r.ok)
        self.assertEqual(r.exit_code, 0)
        self.assertEqual(r.checked_at, "2026-10-01 17:32")
        self.assertEqual(r.snapshot["P1"]["S0"]["display"], "available")
        self.assertEqual(r.attempts, 1)

    # ---- B2：浏览器挂死必须被硬超时打断，而不是让主循环永久阻塞 ----
    def test_hang_is_killed_and_retried_then_given_up(self):
        self._use(WORKER_HANG)
        started = time.time()
        r = self._run(timeout=2)
        elapsed = time.time() - started
        self.assertTrue(r.timed_out, "应判定为超时")
        self.assertEqual(r.attempts, supervisor.MAX_ATTEMPTS)
        self.assertFalse(r.ok)
        # 每次都真的等到了超时，且没有一路拖下去（killpg 生效的证据）
        self.assertGreaterEqual(elapsed, 2 * supervisor.MAX_ATTEMPTS - 1)
        self.assertLess(elapsed, 2 * supervisor.MAX_ATTEMPTS + 15)

    # ---- 致命码不重试 ----
    def test_precheck_failure_is_not_retried(self):
        self._use(worker_body(14, "PRECHECK"))
        r = self._run()
        self.assertEqual(r.exit_code, 14)
        self.assertEqual(r.kind, "PRECHECK")
        self.assertEqual(r.attempts, 1, "预检失败重试也没有意义")

    def test_live_orphan_is_not_retried_nor_killed(self):
        """有活着的 chromium 持有 profile 时，既不重试也不擅自杀它可能正在排障的人。"""
        self._use(worker_body(11, "LOCK_LIVE"))
        r = self._run()
        self.assertEqual(r.exit_code, 11)
        self.assertEqual(r.attempts, 1)

    # ---- 崩溃可重试 ----
    def test_fetch_crash_is_retried(self):
        self._use(worker_body(15, "FETCH_CRASHED"))
        r = self._run()
        self.assertEqual(r.exit_code, 15)
        self.assertEqual(r.attempts, supervisor.MAX_ATTEMPTS)

    # ---- 连 out 都没写出来 ----
    def test_missing_out_is_reported(self):
        self._use(WORKER_NO_OUT)
        r = self._run()
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, supervisor.MAX_ATTEMPTS)
        self.assertTrue(any(k == "NO_RESULT" for k in (r.kind, "")), r.kind)

    def test_bad_json_out_is_reported(self):
        self._use(WORKER_BAD_JSON)
        r = self._run()
        self.assertFalse(r.ok)
        self.assertEqual(r.kind, "BAD_RESULT")

    # ---- profile 重建阶梯 ----
    def test_corrupt_profile_is_discarded_with_backup(self):
        # profile 里放点东西，验证是被备份而不是删除
        with open(os.path.join(self.profile, "Cookies"), "w") as f:
            f.write("precious")
        self._use(worker_body(12, "PROFILE_CORRUPT"))
        r = self._run()
        self.assertTrue(r.rebuilt, "损坏应触发重建")
        self.assertGreater(r.attempts, 1)
        self.assertFalse(os.path.isdir(self.profile), "原 profile 应已被移走")
        backups = [n for n in os.listdir(self.data) if n.startswith("profile.corrupt-")]
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.exists(os.path.join(self.data, backups[0], "Cookies")),
                        "备份里必须还能找到原来的 cookie")

    def test_rebuild_budget_is_limited_per_day(self):
        with open(supervisor._budget_path(self.data), "w") as f:
            json.dump({"date": time.strftime("%Y-%m-%d"), "count": 1}, f)
        self.assertEqual(supervisor.rebuild_budget_left(self.data), 0)

        self._use(worker_body(12, "PROFILE_CORRUPT"))
        r = self._run()
        self.assertFalse(r.rebuilt, "预算用完就不该再动 profile")
        self.assertEqual(r.attempts, 1)
        self.assertTrue(os.path.isdir(self.profile), "profile 应原样保留")

    def test_budget_resets_next_day(self):
        with open(supervisor._budget_path(self.data), "w") as f:
            json.dump({"date": "2020-01-01", "count": 9}, f)
        self.assertEqual(supervisor.rebuild_budget_left(self.data),
                         supervisor.REBUILD_MAX_PER_DAY)


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.health = supervisor.Health(self.data)

    def _cfg(self, webhook=""):
        return {"health_webhook": webhook}

    def test_record_writes_health_file_and_counts_failures(self):
        h = self.health.record(ok=True, kind="OK", round_ms=1500, profile=None)
        self.assertEqual(h["consecutive_failures"], 0)
        self.assertTrue(h["last_success_at"])
        self.assertEqual(h["round_ms"], 1500)

        for i in range(1, 4):
            h = self.health.record(ok=False, kind="FETCH_CRASHED",
                                   error="Target crashed", round_ms=10)
            self.assertEqual(h["consecutive_failures"], i)
        # 失败不能把上次成功时间抹掉 —— 那是我们判断「多久没干活」的唯一依据
        self.assertTrue(h["last_success_at"])

        h = self.health.record(ok=True, kind="OK")
        self.assertEqual(h["consecutive_failures"], 0)

        with open(supervisor.health_path(self.data), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["consecutive_failures"], 0)
        self.assertIn("last_success_epoch", on_disk)
        self.assertIn("disk_free_mb", on_disk)

    def test_no_webhook_means_no_alert(self):
        """未配置告警地址就什么都不做：不复用到业务推送，也不刷日志。"""
        for _ in range(5):
            self.health.record(ok=False, kind="FETCH_CRASHED")
        with mock.patch.object(supervisor.notifier, "send") as send:
            self.assertFalse(self.health.maybe_alert(self._cfg()))
            self.assertFalse(send.called)

    def test_alert_after_threshold_and_throttled(self):
        webhook = "https://api.day.app/hook"
        with mock.patch.object(supervisor.notifier, "send", return_value=(True, "ok")) as send:
            for i in range(supervisor.HEALTH_ALERT_AFTER - 1):
                self.health.record(ok=False, kind="FETCH_CRASHED")
                self.health.maybe_alert(self._cfg(webhook))
            self.assertFalse(send.called, "未达阈值不应报警")

            self.health.record(ok=False, kind="FETCH_CRASHED")
            self.health.maybe_alert(self._cfg(webhook))
            self.assertEqual(send.call_count, 1, "达到阈值应报警一次")

            # 节流窗口内不重复轰炸
            self.health.record(ok=False, kind="FETCH_CRASHED")
            self.health.maybe_alert(self._cfg(webhook))
            self.assertEqual(send.call_count, 1)

    def test_recovery_notification(self):
        webhook = "https://api.day.app/hook"
        with mock.patch.object(supervisor.notifier, "send", return_value=(True, "ok")) as send:
            for _ in range(supervisor.HEALTH_ALERT_AFTER):
                self.health.record(ok=False, kind="FETCH_CRASHED")
            self.health.maybe_alert(self._cfg(webhook))
            self.assertEqual(send.call_count, 1)

            # 恢复正常：应推一条「已恢复」，告知这段时间的库存变动可能没被监测到
            self.health.record(ok=True, kind="OK")
            self.health.maybe_alert(self._cfg(webhook))
            self.assertEqual(send.call_count, 2)
            self.assertIn("已恢复正常", send.call_args[0][1])

    def test_alert_failure_never_raises(self):
        """告警自身的任何异常都不能把主循环拖垮。"""
        for _ in range(supervisor.HEALTH_ALERT_AFTER):
            self.health.record(ok=False, kind="X")
        with mock.patch.object(supervisor.notifier, "send",
                               side_effect=RuntimeError("network down")):
            self.health.maybe_alert(self._cfg("https://hook"))


class SingletonLockTest(unittest.TestCase):
    """上次事故的直接成因：容器 hostname 漂移导致 SingletonLock 永久失效。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = os.path.join(self.tmp.name, "profile")
        os.makedirs(self.profile)

    def _lock(self, value):
        os.symlink(value, os.path.join(self.profile, "SingletonLock"))

    def test_missing_lock(self):
        self.assertEqual(worker.inspect_singleton_lock(self.profile)[0], "missing")

    def test_stale_lock_from_another_host(self):
        """hostname 不符：Chromium 会无条件 exit 21，所以必须能被识别为陈旧。"""
        self._lock("deadbeefcafe-999999")
        state, detail = worker.inspect_singleton_lock(self.profile)
        self.assertEqual(state, "stale")
        self.assertIn("deadbeefcafe", detail)

    def test_stale_lock_when_pid_dead(self):
        self._lock(f"{socket.gethostname()}-999999")
        self.assertEqual(worker.inspect_singleton_lock(self.profile)[0], "stale")

    def test_live_lock_same_host_alive_pid(self):
        """hostname 相符且 pid 仍活着 → 不能清理，否则会同时跑两个浏览器写同一 profile。"""
        self._lock(f"{socket.gethostname()}-{os.getpid()}")
        self.assertEqual(worker.inspect_singleton_lock(self.profile)[0], "live")

    def test_unrecognised_lock_format_is_stale_without_live_process(self):
        self._lock("garbage")
        self.assertEqual(worker.inspect_singleton_lock(self.profile)[0], "stale")

    def test_remove_locks(self):
        for name in worker.LOCK_FILES:
            open(os.path.join(self.profile, name), "a").close()
        worker.remove_singleton_locks(self.profile)
        self.assertEqual(os.listdir(self.profile), [])

    def test_start_error_classification(self):
        lock = Exception("profile appears to be in use by another Chromium process (23339) "
                         "on another computer")
        corrupt = Exception("Corruption: checksum mismatch in leveldb")
        unknown = Exception("kaboom")
        self.assertEqual(worker.classify_start_error(lock), worker.EXIT_LOCK_STALE)
        self.assertEqual(worker.classify_start_error(corrupt), worker.EXIT_PROFILE_CORRUPT)
        self.assertEqual(worker.classify_start_error(unknown), worker.EXIT_START_FAILED)

    def test_precheck_rejects_bad_executable(self):
        kind, msg = worker.precheck(self.profile, "/nonexistent/chromium")
        self.assertEqual(kind, "PRECHECK")
        self.assertIn("chromium", msg)


if __name__ == "__main__":
    unittest.main()
