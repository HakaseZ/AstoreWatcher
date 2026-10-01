#!/usr/bin/env python3
"""取数 worker 子进程 —— 整个项目里唯一会启动浏览器的进程。

**为什么要独立成进程**（详见 docs/plan-watchdog-hardening.md）：

1. `page.evaluate` 在 Playwright sync API 下不受任何 timeout 约束。浏览器一旦挂死，
   调用会永久阻塞，主循环再也不推进一步、也不打日志、连 SIGTERM 都来不及处理。
   只有父进程从**外部** killpg 才能真正打断它。
2. 崩溃、OOM、内存泄漏、句柄泄漏、profile 损坏……这些「逻辑不属于我们、也无法控制」
   的毛病，最终都表现为同一件事：**worker 进程退出**。于是父进程只需一个统一的
   「重启重跑」动作就能兜底 —— 连我们今天还没想到的失败模式一起兜了。

契约：

    调用   python3 worker.py --spec <spec.json> --out <out.json>
    spec   {"profile": str, "location": str, "parts": [str], "executable": str|null}
    out    {"ok","checked_at","snapshot","failed","kind","error"}
    退出码 见下方 EXIT_* 常量

**worker 绝不写 state.json / init.json / health.json**。被 SIGKILL 时它不能留下半截
状态，更不能把「已完成首推」的标记写死 —— 所有持久状态一律由父进程写。
"""

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import time

EXIT_OK = 0          # 取到数据，无失败批次
EXIT_PARTIAL = 3     # 有失败批次（snapshot 可能部分有效）
EXIT_INTERNAL = 9    # worker 自身未捕获异常
EXIT_LOCK_STALE = 10  # 陈旧 SingletonLock，已清理，可重试
EXIT_LOCK_LIVE = 11   # 有活着的 chromium 正持有该 profile：不重试、不擅自杀
EXIT_PROFILE_CORRUPT = 12  # profile 损坏，需由父进程按预算重建
EXIT_START_FAILED = 13     # 启动失败但原因不明
EXIT_PRECHECK = 14   # 环境预检失败（可执行文件/权限/磁盘），不必重试
EXIT_FETCH_CRASHED = 15    # 取数途中浏览器崩溃
EXIT_TERMINATED = 16  # 被父进程主动中断（正常信号，不计入失败）

# Chromium 单例锁三件套。SingletonLock 是符号链接，内容形如 "<hostname>-<pid>"。
LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

# LevelDB / profile 损坏在 stderr 里的特征串。**这是据 Chromium 已知报错推断的，
# 未经本机实测** —— 若发现 rebuild 该触发不触发，请以真实日志校对这里。
CORRUPT_HINTS = (
    "corruption", "corrupt", "invalid argument: unable to open",
    "lock held", "failed to create a processsingleton", "leveldb",
)
LOCK_HINTS = (
    "another chromium process", "profile appears to be in use",
    "singleton", "processsingleton",
)

# 磁盘剩余空间告警线
MIN_FREE_BYTES = 200 * 1024 * 1024

log = logging.getLogger("worker")

# 供 SIGTERM handler 取到当前会话做收尾；handler 里不能做复杂的事。
_CURRENT = {"session": None}


def _handle_term(signum, _frame):
    """收到父进程的中断信号：尽快让 playwright 干净关闭后退出。

    之所以要在 handler 里主动收尾，是因为 Python 收到 SIGTERM 的默认行为是
    **直接终止**，不会执行 atexit —— 那样 chromium 会变成孤儿并继续持有
    SingletonLock，正好制造我们要修的死锁。这里给它一个干净退出 chromium
    的机会；chromium 自己也会从父进程的 killpg 收到 SIGTERM，双保险。
    """
    log.info("worker 收到信号 %s，正在收尾", signum)
    session = _CURRENT.get("session")
    try:
        if session is not None:
            session.close()
    except Exception:
        pass
    os._exit(EXIT_TERMINATED)


def install_signal_handler():
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)


# ---------------------------------------------------------------- 结果写出

def write_result(path, *, ok, checked_at, snapshot, failed, kind, error):
    """原子写 out JSON。父进程靠「文件是否存在」判断 worker 有没有跑完。"""
    payload = {
        "ok": bool(ok),
        "checked_at": checked_at,
        "snapshot": snapshot or {},
        "failed": failed or [],
        "kind": kind,
        "error": error or "",
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".round-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fail(out_path, code, kind, error):
    """写一份失败结果并返回退出码。除被信号中断外，失败也必须留下 out 文件，
    否则父进程无法区分「worker 崩了」和「worker 没写成功」。"""
    log.error("%s：%s", kind, error)
    try:
        write_result(out_path, ok=False, checked_at=None, snapshot={},
                     failed=[], kind=kind, error=error)
    except Exception as e:  # 连 out 都写不了（磁盘满等）：至少让退出码把信息带出去
        log.error("写出 out 失败：%s", e)
    return code


# ---------------------------------------------------------------- 环境预检

def resolve_executable(executable):
    """把 executable 解析成可执行文件路径；不可用则返回 None。"""
    if not executable:
        return None
    if os.path.isabs(executable):
        return executable if os.access(executable, os.X_OK) else None
    return shutil.which(executable)


def precheck(profile, executable):
    """启动前把注定失败的情况挡掉。返回 (kind or None, message)。

    这些都不是重试能解决的：可执行文件不会因为重试就变好，只会让真正的告警
    淹没在重复日志里。
    """
    if not resolve_executable(executable):
        return "PRECHECK", f"chromium 可执行文件不可用：{executable!r}"
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError as e:
        return "PRECHECK", f"profile 目录无法创建：{e}"
    probe = os.path.join(profile, ".write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
    except OSError as e:
        return "PRECHECK", f"profile 目录不可写：{e}"
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    try:
        free = shutil.disk_usage(profile).free
    except OSError as e:
        return "PRECHECK", f"无法读取磁盘剩余空间：{e}"
    if free < MIN_FREE_BYTES:
        return "PRECHECK", f"磁盘剩余空间不足：{free // (1024 * 1024)}MB"
    return None, ""


# ---------------------------------------------------------------- 锁与孤儿进程

def live_chromium_pids(profile):
    """扫 /proc 找出正持有该 profile 的 chromium 进程。

    比读 SingletonLock 可靠得多：锁可能早已陈旧，而这里看到的是**当下真实存在**
    的进程。注意 PID namespace 只覆盖本容器 —— 若另一个容器也挂了同一份 profile，
    这里看不到（那属于配置错误，不在自愈范围内）。
    """
    needle = os.path.abspath(profile)
    found = []
    if not os.path.isdir("/proc"):
        return found
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for arg in cmd.split("\0"):
            if arg.startswith("--user-data-dir="):
                if os.path.abspath(arg.split("=", 1)[1]) == needle:
                    found.append(pid)
                break
    return found


def pid_alive(pid):
    return isinstance(pid, int) and pid > 0 and os.path.exists(f"/proc/{pid}")


def inspect_singleton_lock(profile):
    """判定 profile 的单例锁状态，返回 ("missing"|"stale"|"live", 说明)。

    Chromium 的判定顺序是：**先比 hostname，hostname 不符就直接 exit 21**，连那个
    pid 是否活着都不检查 —— 这正是上次事故的根因（容器 hostname 每次重建都变）。
    hostname 相符时它才会去验 pid 死活。

    所以这里分两步：先扫活进程（权威），再看锁里的记录。只要没有活进程占着，
    锁就是陈旧的、可以清 —— 否则会重现「永久锁死、人工才能恢复」。
    """
    pids = live_chromium_pids(profile)
    if pids:
        return "live", f"检测到正持有该 profile 的 chromium 进程：{pids}"

    lock = os.path.join(profile, "SingletonLock")
    if not os.path.lexists(lock):
        return "missing", ""
    try:
        target = os.readlink(lock)
    except OSError:
        return "missing", ""

    host, _, pid_text = target.rpartition("-")
    if host and pid_text.isdigit():
        if host == socket.gethostname() and pid_alive(int(pid_text)):
            return "live", f"锁记录 {target!r}，且 pid {pid_text} 仍存活"
        return "stale", (f"锁记录 {target!r}，本机 {socket.gethostname()!r}；"
                         f"pid {pid_text} 已不存在（hostname 漂移或进程已死）")
    # 认不出的格式同样视为陈旧：没有活进程占有它，留着只会害 Chromium 拒绝启动
    return "stale", f"无法识别的锁内容 {target!r}，且无进程持有该 profile"


def remove_singleton_locks(profile):
    """清掉 Chromium 的单例锁三件套。只在确认没有活进程时才被调用。"""
    removed = []
    for name in LOCK_FILES:
        try:
            os.unlink(os.path.join(profile, name))
            removed.append(name)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("清理 %s 失败：%s", name, e)
    return removed


# ---------------------------------------------------------------- 异常归类

def classify_start_error(exc):
    """把启动异常映射到退出码，让父进程知道该重试还是该告警。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(h in text for h in CORRUPT_HINTS):
        return EXIT_PROFILE_CORRUPT
    if any(h in text for h in LOCK_HINTS):
        return EXIT_LOCK_STALE
    return EXIT_START_FAILED


KIND_BY_CODE = {
    EXIT_LOCK_STALE: "LOCK_STALE",
    EXIT_PROFILE_CORRUPT: "PROFILE_CORRUPT",
    EXIT_START_FAILED: "START_FAILED",
    EXIT_FETCH_CRASHED: "FETCH_CRASHED",
    EXIT_INTERNAL: "INTERNAL",
}


# ---------------------------------------------------------------- 主流程

def close_session(session):
    try:
        if session is not None:
            session.close()
    except Exception as e:
        log.warning("关闭浏览器会话失败（忽略）：%s", e)


def run(spec, out_path):
    """跑一次完整取数，返回退出码。out 文件里带上 worker 侧算出的 checked_at。"""
    from browser import BrowserSession  # 延迟导入：只有 worker 需要 playwright

    import config as config_mod

    profile = spec["profile"]
    location = spec.get("location") or config_mod.DEFAULT_LOCATION
    parts = spec.get("parts") or []
    executable = spec.get("executable")

    kind, message = precheck(profile, executable)
    if kind:
        return _fail(out_path, EXIT_PRECHECK, kind, message)

    lock_state, detail = inspect_singleton_lock(profile)
    if lock_state == "live":
        # 活的孤儿进程：可能是有人在手动跑 `once` 排障，绝不擅自杀。
        return _fail(out_path, EXIT_LOCK_LIVE, "LOCK_LIVE", detail)
    if lock_state == "stale":
        removed = remove_singleton_locks(profile)
        log.warning("清理陈旧单例锁（%s）：%s", ",".join(removed) or "无文件", detail)

    session = BrowserSession(profile, location=location, executable=executable)
    _CURRENT["session"] = session
    try:
        session.start()
    except Exception as e:
        code = classify_start_error(e)
        _CURRENT["session"] = None
        close_session(session)
        return _fail(out_path, code, KIND_BY_CODE[code],
                     f"浏览器启动失败：{type(e).__name__}: {e}")

    try:
        snapshot, failed = session.fetch_all(parts)
    except Exception as e:  # Target crashed / Target closed / 浏览器被外部杀掉
        _CURRENT["session"] = None
        close_session(session)
        return _fail(out_path, EXIT_FETCH_CRASHED, "FETCH_CRASHED",
                     f"取数途中浏览器失联：{type(e).__name__}: {e}")
    finally:
        _CURRENT["session"] = None
        close_session(session)

    # checked_at 必须在**这里**算：同一份 snapshot 会分发给多个推送目标，
    # 各次推送之间相隔十几秒，但它们的检查时刻必须是同一个。
    checked_at = config_mod.hk_now()
    code = EXIT_PARTIAL if failed else EXIT_OK
    write_result(out_path, ok=bool(snapshot), checked_at=checked_at,
                 snapshot=snapshot, failed=failed,
                 kind="OK" if not failed else "PARTIAL", error="")
    log.info("本轮完成：%d 个 SKU，失败批次 %d，检查时间 %s",
             len(snapshot), len(failed), checked_at)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="worker.py")
    parser.add_argument("--spec", required=True, help="父进程写的请求 JSON 路径")
    parser.add_argument("--out", required=True, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [worker] %(message)s")
    install_signal_handler()

    started = time.time()
    try:
        with open(args.spec, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        return _fail(args.out, EXIT_INTERNAL, "INTERNAL", f"读取 spec 失败：{e}")

    try:
        code = run(spec, args.out)
    except Exception as e:  # worker 自身的异常绝不能变成无声无息的死
        return _fail(args.out, EXIT_INTERNAL, "INTERNAL",
                     f"worker 未捕获异常：{type(e).__name__}: {e}")
    log.info("worker 退出码 %d，耗时 %.1fs", code, time.time() - started)
    return code


if __name__ == "__main__":
    sys.exit(main())
