"""常驻轮询：查库存 → 与上次状态比对 → 按各推送目标关注的 SKU 发通知。

配置由 config.py 提供（支持热加载：每轮按 mtime 重新读取）。
"""

import json
import logging
import os
import random
import signal
import sys
import tempfile
import time

import config as config_mod
import notifier
import state as state_mod
import supervisor

log = logging.getLogger("poller")

DEFAULT_DATA_DIR = "/data"

# 连续多少轮「浏览器正常跑完但一个数据都没拿到」才考虑重建 profile。
# profile 里的 shield cookie 很珍贵，不能因为一次接口抖动就丢掉。
EMPTY_ROUNDS_BEFORE_REBUILD = 3

_SHUTDOWN = False


def _write_stores(state_path, snapshot):
    """把本轮查到的门店名（来自 Apple 接口 storeName）持久化到 data/stores.json，
    供配置界面作为「推送门店」选项。发现到的真实店名优先，已存在的保留顺序。"""
    discovered = set()
    for part, stores in (snapshot or {}).items():
        if isinstance(stores, dict):
            discovered.update(stores.keys())
    if not discovered:
        return
    directory = os.path.dirname(os.path.abspath(state_path))
    path = os.path.join(directory, "stores.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = []
    merged = list(prev) if isinstance(prev, list) else []
    for s in sorted(discovered):
        if s not in merged:
            merged.append(s)
    data = json.dumps(merged, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".stores-", suffix=".tmp")
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



def load_sku_meta(path):
    """{part: {"model", "color", "capacity"}}。"""
    import json

    with open(path, encoding="utf-8") as f:
        return {s["part"]: s for s in json.load(f)}


def _target_parts(cfg, sku_meta=None):
    """所有启用目标关注的 part（去重，保持顺序）。未知 part 直接剔除。

    sku_meta 为空（调用方没给 SKU 表）时不过滤 —— 那时也没有合法清单可依。
    """
    parts = []
    for t in cfg.get("targets") or []:
        if not t.get("enabled", True):
            continue
        for p in t.get("parts") or []:
            if p not in parts and (not sku_meta or p in sku_meta):
                parts.append(p)
    return parts


def run_once(cfg, sku_meta, profile, state_path, init_path, executable=None, data_dir=None):
    """跑一轮查询 + 推送。返回 supervisor.RoundResult。

    浏览器相关的脏活全部交给 supervisor / worker 子进程（见
    docs/plan-watchdog-hardening.md）：本函数不再直接接触 playwright，
    浏览器再怎么崩溃、挂死、泄漏，都不会在这个进程里留下状态。
    """
    prev = state_mod.load(state_path)
    # baseline 仅作本轮判断用的只读快照；真正的写盘走下面加锁的 update_init，
    # 避免数十秒浏览器轮询期间把 web 进程并发写的标记覆盖掉（见 state.update_init）。
    baseline = state_mod.load_init(init_path)
    to_remove = set()          # 本轮要摘除的首推标记（禁用/删除/缺失）
    to_add = {}                # 本轮要写入的首推标记：{tid: {since, parts}}
    now = config_mod.hk_now("%Y-%m-%dT%H:%M:%S")

    # 计算标记增删（放在最前，确保即使所有目标都被禁用也能及时清标记）：
    # - 已被删除 / 禁用的目标 → 摘除标记，使其重新启用时再次走「首次全量推送」（edge B）
    # - 旧格式（值为字符串时间戳）→ 迁移为 {since, parts}，避免升级后把已有 SKU 当新 SKU 重推
    targets_by_id = {t.get("id"): t for t in (cfg.get("targets") or [])}
    for tid, val in list(baseline.items()):
        t = targets_by_id.get(tid)
        if t is None or not t.get("enabled", True):
            to_remove.add(tid)
            continue
        if isinstance(val, str):  # 旧格式迁移
            to_add[tid] = {"since": val, "parts": list(t.get("parts") or [])}

    parts = _target_parts(cfg, sku_meta)
    if not parts:
        log.info("没有启用任何推送目标或没有勾选 SKU，本轮跳过")
        if to_remove:
            state_mod.update_init(init_path, remove=to_remove)
        # 没有可监测的目标不是故障，不该触发告警
        return supervisor.RoundResult(ok=True, kind="NO_PARTS")

    result = supervisor.run_round(profile, parts,
                                  location=cfg.get("location", config_mod.DEFAULT_LOCATION),
                                  executable=executable, data_dir=data_dir)
    snapshot = result.snapshot
    failed = result.failed
    checked_at = result.checked_at
    if not snapshot:
        # 空快照护栏：不推送、也不写 init 标记，避免发出「共 0 个 SKU」的废纸推送
        # 并把目标锁死在「已首推」状态。健康和重建判定交由 loop 负责。
        log.warning("本轮未取到任何数据（%s：%s）", result.kind or "空快照",
                    result.error or "无失败批次")
        return result

    _write_stores(state_path, snapshot)

    for t in cfg.get("targets") or []:
        if not t.get("enabled", True):
            continue
        tid = t.get("id")
        # 刻意叫 t_parts 而不是 parts：外层 parts 是「全部启用目标的 SKU」，
        # 同名会被这里覆盖，循环后若再读 parts 就会拿到最后一个目标的（很难发现的坑）。
        t_parts = t.get("parts") or []
        stores = t.get("stores") or []

        # SKU 与门店均为必填（前端已拦截保存，这里再兜底防止空配置误推全部）
        if not t_parts or not stores:
            log.warning("目标「%s」未选择 SKU 或门店（两者均为必填），本轮跳过",
                        t.get("name") or tid or "（无名）")
            continue

        # 首次加入：推送一次全量状态（含无货），并记录已推送的 SKU，之后只推变化
        if tid and tid not in baseline:
            # 空快照护栏：本轮 Apple 接口没返回任何关注中的 SKU（偶发空响应 /
            # SSL 抖动）时，不推送、也不写 init 标记，下轮拿到真实数据再推全量，
            # 避免发出「共 0 个 SKU」废纸推送并把目标锁死。
            matched = [p for p in t_parts if p in snapshot]
            if not matched:
                log.warning("目标「%s」本轮未匹配到任何 SKU（Apple 接口可能暂未返回），"
                            "暂不推送，下轮重试", t.get("name") or tid)
                continue
            title, body = notifier.format_snapshot(snapshot, t_parts, sku_meta, stores)
            # 首推全量：仅当所选范围内有货才带下单链接，全无货则不带（与变动推送一致）
            first_url = (t.get("order_url") or notifier.APPLE_HOME) \
                if notifier.snapshot_has_available(snapshot, t_parts, stores) else None
            ok, detail = notifier.send(
                t.get("bark_url"), title, body,
                icon=notifier.APPLE_ICON,
                url=first_url,
                checked_at=checked_at,
            )
            log.info("目标「%s」首次全量推送：%s", t.get("name") or tid, "成功" if ok else f"失败 {detail}")
            if ok:  # 失败则不落标记，下一轮重试全量推送
                to_add[tid] = {"since": now, "parts": list(t_parts)}
            continue

        # 已初始化：拆成「已有 SKU 的变化」+「新增 SKU 的当前状态」
        seen = set((baseline.get(tid) or {}).get("parts") or [])
        new_parts = [p for p in t_parts if p not in seen]
        existing = [p for p in t_parts if p in seen]

        # edge A：新增 SKU → 只推该 SKU 的当前状态（含无货），不推已有 SKU
        if new_parts:
            new_entries = state_mod.initial_snapshot(snapshot, new_parts)
            results = notifier.push_target(t, new_entries, sku_meta, checked_at=checked_at)
            ok = sum(1 for r in results if r[0])
            log.info("目标「%s」新增 SKU 首推 %d 条，成功 %d",
                     t.get("name") or tid, len(results), ok)
            for r in results:
                if not r[0]:
                    log.error("推送失败：%s", r[1])
            # 无实际推送（门店过滤后为空）或全部成功才记为已见；否则下一轮重试
            if not results or (ok == len(results)):
                to_add[tid] = {"since": (baseline.get(tid) or {}).get("since") or now,
                               "parts": list(t_parts)}

        # 已有 SKU：只推库存变动
        if existing:
            changes = state_mod.diff(prev, snapshot, existing, t.get("notify_on") or ["available"])
            if not changes:
                continue
            results = notifier.push_target(t, changes, sku_meta, checked_at=checked_at)
            ok = sum(1 for r in results if r[0])
            log.info("目标「%s」推送 %d 条，成功 %d", t.get("name") or tid, len(results), ok)
            for r in results:
                if not r[0]:
                    log.error("推送失败：%s", r[1])

    if to_remove or to_add:
        state_mod.update_init(init_path, add=to_add, remove=to_remove)
    state_mod.save(state_path, state_mod.apply(prev, snapshot))
    return result


def _install_signal_handler():
    """SIGTERM 到达时**立刻掐断**正在跑的那一轮，而不是等它跑完。

    为什么不能等：Docker 的 stop_grace_period（默认只有 10s，本 compose 设 60s）
    远小于一轮的最坏耗时（225s）。真等到这一轮自然结束，容器一定会被 SIGKILL，
    而 SIGKILL 会让 chromium 来不及释放 SingletonLock —— 正好制造我们要修的那个死锁。
    代价只是当前这一轮数据丢掉，下一轮几分钟内补上。
    """
    def handler(signum, frame):
        global _SHUTDOWN
        _SHUTDOWN = True
        log.info("收到信号 %s，正在中断当前轮次", signum)
        supervisor.interrupt_worker(f"父进程收到信号 {signum}")

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, handler)
        except Exception:
            pass


def _sleep_until_next_round(seconds):
    """分段 sleep：让 SIGTERM 能在秒级生效。

    一整段的长 sleep 会把容器退出拖到 grace 超时之后；切成一秒一段后，
    信号一来最多再等 1 秒就能进入下一轮退出判定。
    """
    deadline = time.time() + max(0.0, seconds)
    while not _SHUTDOWN:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def loop(data_dir=DEFAULT_DATA_DIR, skus_path="skus_hk.json", once=False):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _install_signal_handler()

    cfg_path = os.path.join(data_dir, "config.json")
    state_path = os.path.join(data_dir, "state.json")
    init_path = os.path.join(data_dir, "init.json")  # 已完成首轮全量推送的 target 标记
    profile = os.path.join(data_dir, "profile")
    sku_meta = load_sku_meta(skus_path)
    health = supervisor.Health(data_dir)
    empty_rounds = 0

    while True:
        cfg = config_mod.load_cached(cfg_path)
        try:
            result = run_once(cfg, sku_meta, profile, state_path, init_path, data_dir=data_dir)
            log.info("本轮完成：%d 个 SKU，失败批次 %d%s", len(result.snapshot),
                     len(result.failed),
                     f"（检查时间 {result.checked_at}）" if result.checked_at else "")
        except Exception as e:
            # 单轮异常不能让常驻进程死掉 —— 但必须被**看见**（记进 health 并按阈值告警），
            # 上次事故的教训正是异常被默默吞掉导致数月静默失效。
            result = supervisor.RoundResult(ok=False, kind="INTERNAL",
                                            error=f"{type(e).__name__}: {e}")
            log.exception("本轮异常：%s", e)

        # 没有任何可监测的目标不算故障，不该触发告警
        if result.kind == "NO_PARTS":
            health.record(ok=True, kind=result.kind, profile=profile)
        else:
            health.record(ok=result.ok,
                          kind=result.kind or ("OK" if result.ok else "UNKNOWN"),
                          error=result.error, round_ms=result.round_ms, profile=profile)
        health.maybe_alert(cfg)

        # 「浏览器正常跑完、却一个数据都没拿到」累计到阈值且今日还有预算时才动 profile。
        # 进程级故障（超时/崩溃）不参与计数 —— 那不是 profile 的问题。
        browser_ok = result.kind in ("OK", "PARTIAL", "NO_PARTS")
        if result.ok or not browser_ok:
            empty_rounds = 0
        else:
            empty_rounds += 1
            if empty_rounds >= EMPTY_ROUNDS_BEFORE_REBUILD:
                if supervisor.rebuild_budget_left(data_dir) > 0:
                    log.warning("连续 %d 轮未取到数据，备份并重建 profile 冷启动", empty_rounds)
                    supervisor.discard_profile(profile, data_dir)
                else:
                    log.error("连续 %d 轮未取到数据，但今日重建预算已用完，暂不重建",
                              empty_rounds)
                empty_rounds = 0

        if once or _SHUTDOWN:
            return 0
        _sleep_until_next_round(
            cfg.get("interval_sec", config_mod.DEFAULT_INTERVAL_SEC)
            + random.uniform(0, config_mod.RANDOM_JITTER_MAX))


if __name__ == "__main__":
    sys.exit(loop(once="--once" in sys.argv))
