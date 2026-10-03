#!/usr/bin/env python3
"""监督层：把「跑一轮取数」这件脏活丢给 worker 子进程，并对它的死法逐个兜底。

这个模块**刻意不 import playwright** —— 浏览器的一切都关在 worker 里：
  1. worker 挂死 / 崩溃 / 泄漏之后，父进程手里不残留任何浏览器状态，重启即彻底干净。
  2. 本模块可以在没有 playwright 的环境里被测试（tests/test_supervisor.py 用假 worker）。

健康uardard 那一层的必要性来自上次事故：Watcher 整整几个月没取到任何数据，进程却
一直「健在」，exception 被吞掉也没人知道。所以看不见的失效等于没有修 —— 这里既写
health.json（给 docker healthcheck 与配置页看），也主动推 Bark 告警（给手机看）。
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import config as config_mod
import notifier

log = logging.getLogger("supervisor")

# 单轮硬超时。推导：启动+首次开页 75s（含 60s goto）＋每批最坏 45s × 批次 ＋收尾 15s。
# 32 个 SKU 按 15 一批 = 3 批 → 75 + 135 + 15 = 225s。正常轮次 30-60s，不会误杀。
ROUND_TIMEOUT = 225
# 进程收到 SIGTERM 后愿意等它体面收尾的时间，超时就必须 SIGKILL 兜底
TERM_GRACE_SEC = 5

MAX_ATTEMPTS = 3
REBUILD_MAX_PER_DAY = 1

# worker 退码的对端常量。刻意不 import worker（它会拉起 playwright），保持本模块无浏览器依赖。
EXIT_OK = 0
EXIT_PARTIAL = 3

# 可再次尝试的退出码：都是「换个进程说不定就好了」的情况
RETRYABLE_CODES = (9, 10, 13, 15)  # INTERNAL / LOCK_STALE / START_FAILED / FETCH_CRASHED
# 不必重试的退出码：重试一万次也是同一个结果，只会淹没真正的告警
FATAL_CODES = (11, 14)  # LOCK_LIVE / PRECHECK
REBUILDABLE_CODES = (12,)  # PROFILE_CORRUPT

HEALTH_ALERT_AFTER = 3       # 连续失败几次触发告警
ALERT_THROTTLE_SEC = 1800    # 同一轮故障的告警节流窗口（30 分钟）

_PROFILE_WARN_BYTES = 500 * 1024 * 1024  # profile 膨胀告警线（A7）

# 当前活着的 worker，供 SIGTERM handler 立刻掐断（见 interrupt_worker）
_ACTIVE = {"proc": None}


@dataclass
class RoundResult:
    """一轮取数的结果。`ok=False` 时 snapshot 一定是空的。"""
    snapshot: dict = field(default_factory=dict)
    failed: list = field(default_factory=list)
    checked_at: str = ""
    ok: bool = False
    exit_code: int = -1
    kind: str = ""
    error: str = ""
    attempts: int = 0
    timed_out: bool = False
    rebuilt: bool = False
    round_ms: int = 0


def worker_cmd():
    """返回启动 worker 的命令。可用环境变量 ASTORE_WORKER 覆盖（测试用假 worker）。"""
    override = os.environ.get("ASTORE_WORKER")
    if override:
        return [sys.executable, override]
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, "worker.py")]


# ---------------------------------------------------------------- 进程管理

def _atomic_write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def kill_worker_group(proc, grace=TERM_GRACE_SEC):
    """掐断 worker 及其整棵进程树（chromium 在内）。

    顺序必须是 **SIGTERM → 等 → SIGKILL**：SIGTERM 让 chromium 走正常 teardown，
    干净释放 SingletonLock 并 flush profile 的 LevelDB；直接 SIGKILL 会下一次
    启动就踩到陈旧锁或 LevelDB 损坏 —— 等于自己制造我们要修的那个故障。
    但 TERM 必须限时，因为挂死的 chromium 根本不响应 TERM。
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        # start_new_session=True 保证 worker 自成进程组领导（pgid == pid），
        # 因此这里 killpg 不会误伤父进程自己所在的组。
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    deadline = time.time() + grace
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    while proc.poll() is None:
        time.sleep(0.1)


def interrupt_worker(reason=""):
    """供父进程的 SIGTERM handler 调用：立刻掐断正在跑的那一轮。

    这是 `stop_grace_period: 60s` 能成立的前提 —— 若等到这一轮自然跑完（最坏 225s），
    docker stop 的 10s/60s 宽限一定不够，又会被 SIGKILL，又留下陈旧锁。
    代价是当前这一轮数据丢弃，下一轮几分钟内补上，对库存监测没有实质影响。
    """
    proc = _ACTIVE.get("proc")
    if proc is None:
        return
    log.info("中断当前轮次（%s）", reason or "收到退出信号")
    kill_worker_group(proc)


def reap_orphans():
    """回收所有已死的孤儿子进程（详见下方调用处的事故说明）。

    容器里 `main.py poll` 是 PID 1。worker 用 start_new_session 自成进程组，
    chromium 是它的后代；worker 一退出，chromium 就被内核 reparent 到 PID 1。
    若 PID 1 从不 wait，这些死掉的 chromium 就永远挂成僵尸，把 PID cgroup 一点点
    撑满，最终连 fork 都失败（Resource temporarily unavailable / Cannot fork），
    表现为反复 START_FAILED 的死亡螺旋。这里在每轮结束后扫一遍死掉的子进程回收掉，
    防止无限累积。

    为什么安全：本函数只在 worker 已被 proc.wait 回收之后调用，所以不会误收 worker
    本身；它只回收「worker 退出后残留的 chromium 孤儿」。
    """
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:  # 没有更多死掉的子进程了
                break
    except ChildProcessError:  # 当前没有任何子进程可回收
        pass


def _spawn(cmd, spec_path, out_path):
    return subprocess.Popen(
        cmd + ["--spec", spec_path, "--out", out_path],
        # 关键：自成进程组，之后才能整组 killpg 连带 chromium；
        # stdout/stderr 继承父进程，日志直接进 docker log。
        start_new_session=True,
    )


def _run_once(profile, parts, location, executable, data_dir, timeout):
    """跑一次 worker，返回 (RoundResult, exit_code_or_None)。"""
    fd, spec_path = tempfile.mkstemp(dir=data_dir, prefix=".spec-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"profile": profile, "location": location,
                   "parts": parts, "executable": executable}, f, ensure_ascii=False)
    out_path = os.path.join(data_dir, f".round-{os.getpid()}-{int(time.time() * 1000)}.json")

    started = time.time()
    result = RoundResult()
    proc = None
    try:
        proc = _spawn(worker_cmd(), spec_path, out_path)
        _ACTIVE["proc"] = proc
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result.timed_out = True
            log.error("worker 超过 %ds 未完成，判定为浏览器挂死，强制掐断", timeout)
            kill_worker_group(proc)
            result.kind, result.error = "TIMEOUT", f"worker 超过 {timeout}s 未完成"
        code = proc.returncode
        result.exit_code = code if code is not None else -1
        # worker 已被 proc.wait 回收；此刻它残留的 chromium 孤儿已 reparent 到本进程
        # （PID 1），趁热回收掉，否则会累积成僵尸撑满 PID cgroup（见 reap_orphans）。
        reap_orphans()
        result.round_ms = int((time.time() - started) * 1000)
    finally:
        _ACTIVE["proc"] = None
        for path in (spec_path,):
            try:
                os.unlink(path)
            except OSError:
                pass

    try:
        with open(out_path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        # 连 out 都没写出来 —— worker 在写完之前就死了（SIGKILL / OOM / 断电）
        result.kind = result.kind or "NO_RESULT"
        result.error = result.error or f"worker 未产出结果（退出码 {result.exit_code}）"
        return result, None
    except ValueError as e:
        result.kind, result.error = "BAD_RESULT", f"worker 输出不是合法 JSON：{e}"
        return result, None
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    result.snapshot = payload.get("snapshot") or {}
    result.failed = payload.get("failed") or []
    result.checked_at = payload.get("checked_at") or ""
    result.kind = payload.get("kind") or ""
    result.error = payload.get("error") or ""
    result.ok = bool(result.snapshot)
    return result, result.exit_code


# ---------------------------------------------------------------- 重建预算

def _budget_path(data_dir):
    return os.path.join(data_dir, "profile_rebuild.json")


def rebuild_budget_left(data_dir):
    """profile 重建是最后手段：cookie 没了可能被判机器人，所以按天限额。"""
    path = _budget_path(data_dir)
    today = time.strftime("%Y-%m-%d")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if data.get("date") != today:
        return REBUILD_MAX_PER_DAY
    return max(0, REBUILD_MAX_PER_DAY - int(data.get("count") or 0))


def discard_profile(profile, data_dir):
    """把疑似损坏的 profile 重命名备份后交给下一次启动重建。返回新路径或 None。"""
    if not os.path.isdir(profile):
        return None
    path = _budget_path(data_dir)
    today = time.strftime("%Y-%m-%d")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    data["count"] = int(data.get("count") or 0) + 1
    try:
        _atomic_write_json(path, data)
    except OSError as e:
        log.error("写入重建预算失败：%s", e)

    backup = f"{profile}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
    try:
        os.replace(profile, backup)  # 同目录 rename，原子；保留 cookie 供人工捞回
        log.warning("profile 疑似损坏，已备份为 %s 并准备冷启动重建", backup)
        return backup
    except OSError as e:
        log.error("备份 profile 失败（%s），退化为直接删除", e)
        shutil.rmtree(profile, ignore_errors=True)
        return None


# ---------------------------------------------------------------- 一轮对外接口

def run_round(profile, parts, location=None, executable=None, data_dir=None,
              timeout=ROUND_TIMEOUT):
    """跑一轮取数，内含重试与重建阶梯。返回 RoundResult。

    判定顺序：
      LOCK_LIVE / PRECHECK  → 立即收工（不该重试，也不该动 profile）
      PROFILE_CORRUPT       → 还有预算就重建一次再试，否则收工
      其余可重试码          → 换一个新 worker 进程再试
      超过 MAX_ATTEMPTS     → 收工，交上层计数告警
    """
    location = location or config_mod.DEFAULT_LOCATION
    result = RoundResult()
    rebuilt = False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        cur, code = _run_once(profile, parts, location, executable, data_dir, timeout)
        cur.attempts = attempt
        cur.rebuilt = rebuilt

        # 取到数据就算本轮完成（哪怕有失败批次）；往下走的都是"进程级故障"
        if code in (EXIT_OK, EXIT_PARTIAL):
            if not cur.snapshot and cur.failed:
                # 全批失败多半是 shield 判机器人。worker 内每批已 reheat 重试过，
                # 这里不再折腾 profile，交由上层按**跨轮**累计决定是否重建
                log.warning("本轮全部批次失败（HTTP %s），交上层累计判断",
                            (cur.failed[0] or {}).get("status"))
            return cur

        if cur.timed_out:
            log.error("第 %d 次尝试超时（浏览器挂死），已掐断进程组并重开", attempt)
            result = cur
            continue
        if code in FATAL_CODES:
            log.error("不可重试的失败（%s）：%s", cur.kind, cur.error)
            return cur
        if code in REBUILDABLE_CODES:
            if rebuilt or rebuild_budget_left(data_dir) <= 0:
                log.error("profile 损坏但今日重建预算已用完，停止重试：%s", cur.error)
                return cur
            discard_profile(profile, data_dir)
            rebuilt = True
            continue

        result = cur
        log.warning("第 %d 次尝试失败（退出码 %s / %s）：%s", attempt, code, cur.kind, cur.error)

    log.error("已尝试 %d 次仍未成功，放弃本轮", MAX_ATTEMPTS)
    return result


# ---------------------------------------------------------------- 健康与告警

def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def health_path(data_dir):
    return os.path.join(data_dir, "health.json")


def load_health(data_dir):
    try:
        with open(health_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Health:
    """每轮跑完记录一次健康状态，并在跨过阈值时发告警。

    为什么单独一层：上次事故里 watcher 明明几个月没取到数据，进程却一直「健在」。
    自愈逻辑再好，看不见的失效仍然等于没修 —— 所以这里既写 health.json
    （给 docker healthcheck 和配置页看），也主动推 Bark 告警（给手机看）。
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.failures = 0
        self.last_alert_at = 0.0
        # 「已告警」状态要落盘才能跨重启存活：容器重启后 Health 是个新实例，
        # 只看内存状态的话，故障恢复时永远发不出「已恢复正常」那一条。
        self.alerted = bool(load_health(data_dir).get("alerted"))

    def record(self, *, ok, kind="", error="", round_ms=0, profile=None):
        """把本轮结果写进 health.json。返回写出的字典。"""
        now = time.time()
        prev = load_health(self.data_dir)
        if ok:
            self.failures = 0
            last_success_at = config_mod.hk_now("%Y-%m-%d %H:%M:%S")
            last_success_epoch = now
        else:
            self.failures += 1
            last_success_at = prev.get("last_success_at", "")
            last_success_epoch = prev.get("last_success_epoch", 0.0)

        payload = {
            "last_attempt_at": config_mod.hk_now("%Y-%m-%d %H:%M:%S"),
            "last_success_at": last_success_at,
            "last_success_epoch": last_success_epoch,
            "consecutive_failures": self.failures,
            "last_error_kind": kind,
            "last_error": error,
            "alerted": self.alerted,
            "round_ms": round_ms,
            "profile_bytes": (_dir_bytes(profile) if profile and os.path.isdir(profile) else 0),
        }
        try:
            payload["disk_free_mb"] = shutil.disk_usage(self.data_dir).free // (1024 * 1024)
        except OSError:
            payload["disk_free_mb"] = 0

        try:
            _atomic_write_json(health_path(self.data_dir), payload)
        except OSError as e:  # 连健康状态都写不了：至少日志里要有
            log.error("写入 health.json 失败：%s", e)
        if payload["profile_bytes"] > _PROFILE_WARN_BYTES:
            log.warning("profile 已膨胀到 %dMB，建议排查或重建",
                        payload["profile_bytes"] // (1024 * 1024))
        return payload

    def maybe_alert(self, cfg):
        """按需推送故障/恢复告警。告警自身的任何异常都不能冒泡到主循环。"""
        webhook = (cfg or {}).get("health_webhook") or ""
        if not webhook:
            return False  # 未配置：只在配置页提示，不做任何降级（已与用户确认）
        now = time.time()
        try:
            if self.failures >= HEALTH_ALERT_AFTER:
                if now - self.last_alert_at >= ALERT_THROTTLE_SEC:
                    self._send(webhook, "Watcher 已连续失败",
                               f"连续 {self.failures} 轮取数失败，请检查容器日志。")
                    self.last_alert_at = now
                    self.alerted = True
                    self._persist_alerted()
            elif self.alerted and self.failures == 0:
                self._send(webhook, "Watcher 已恢复正常", "取数已恢复，期间的库存变动可能未被监测到。")
                self.alerted = False
                self.last_alert_at = 0.0
                self._persist_alerted()
        except Exception as e:
            log.error("推送健康告警失败（忽略）：%s", e)
        return self.alerted

    def _persist_alerted(self):
        """把刚改过的 alerted 立刻写回 health.json。

        主循环是先 record()（写盘用的是**改之前**的 alerted）再 maybe_alert()（这才改
        alerted）的，若不在改完后立刻补写，就要等下一轮 record() 才落盘 —— 中间这
        一轮（约 2 分钟）内一旦容器重启，alerted 就丢了，恢复时再也发不出
        「已恢复正常」那条。这里是 patch 式改写：只动 alerted 一个字段，
        不覆盖 watcher 刚写的其它字段。
        """
        try:
            prev = load_health(self.data_dir)
            if not isinstance(prev, dict):
                prev = {}
            prev["alerted"] = self.alerted
            _atomic_write_json(health_path(self.data_dir), prev)
        except OSError as e:
            log.error("写入 alerted 失败（忽略）：%s", e)

    def _send(self, webhook, title, body):
        ok, detail = notifier.send(webhook, title, body, icon=notifier.APPLE_ICON,
                                   level="timeSensitive", group="AstoreWatcher")
        log.info("健康告警「%s」：%s", title, "已送达" if ok else f"发送失败 {detail}")
