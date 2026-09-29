"""常驻轮询：查库存 → 与上次状态比对 → 按各推送目标关注的 SKU 发通知。

配置由 config.py 提供（支持热加载：每轮按 mtime 重新读取）。
"""

import datetime
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
from browser import BrowserSession

log = logging.getLogger("poller")

DEFAULT_DATA_DIR = "/data"

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


def run_once(cfg, sku_meta, profile, state_path, init_path, executable=None):
    """跑一轮查询 + 推送。返回 (成功取到的 snapshot, 失败批次列表)。"""
    prev = state_mod.load(state_path)
    inited = state_mod.load_init(init_path)
    touched_init = False
    now = datetime.datetime.now().isoformat(timespec="seconds")

    # 清理 / 迁移 init 标记（放在最前，确保即使所有目标都被禁用也能及时清标记）：
    # - 已被删除的目标 → 移除标记
    # - 已被禁用的目标 → 移除标记，使其重新启用时再次走「首次全量推送」（edge B）
    # - 旧格式（值为字符串时间戳）→ 迁移为 {since, parts}，避免升级后把已有 SKU 当新 SKU 重推
    targets_by_id = {t.get("id"): t for t in (cfg.get("targets") or [])}
    for tid in list(inited):
        t = targets_by_id.get(tid)
        if t is None or not t.get("enabled", True):
            inited.pop(tid, None)
            touched_init = True
            continue
        if isinstance(inited[tid], str):  # 旧格式迁移
            inited[tid] = {"since": inited[tid], "parts": list(t.get("parts") or [])}
            touched_init = True

    parts = _target_parts(cfg, sku_meta)
    if not parts:
        log.info("没有启用任何推送目标或没有勾选 SKU，本轮跳过")
        if touched_init:
            state_mod.save_init(init_path, inited)
        return {}, []

    session = BrowserSession(profile, location=cfg.get("location", "中環"),
                             executable=executable)
    session.start()
    try:
        snapshot, failed = session.fetch_all(parts)
        # 全批失败：多半是 profile 被污染，丢弃重建再试一轮
        if failed and not snapshot:
            log.warning("全部批次失败（HTTP %s），重建 profile 后重试", failed[0]["status"])
            session.close()
            session.rebuild_profile()
            session.start()
            snapshot, failed = session.fetch_all(parts)
    finally:
        session.close()

    if not snapshot:
        return snapshot, failed

    _write_stores(state_path, snapshot)

    for t in cfg.get("targets") or []:
        if not t.get("enabled", True):
            continue
        tid = t.get("id")
        parts = t.get("parts") or []
        stores = t.get("stores") or []

        # SKU 与门店均为必填（前端已拦截保存，这里再兜底防止空配置误推全部）
        if not parts or not stores:
            log.warning("目标「%s」未选择 SKU 或门店（两者均为必填），本轮跳过",
                        t.get("name") or tid or "（无名）")
            continue

        # 首次加入：推送一次全量状态（含无货），并记录已推送的 SKU，之后只推变化
        if tid and tid not in inited:
            # 空快照护栏：本轮 Apple 接口没返回任何关注中的 SKU（偶发空响应 /
            # SSL 抖动）时，不推送、也不写 init 标记，下轮拿到真实数据再推全量，
            # 避免发出「共 0 个 SKU」废纸推送并把目标锁死。
            matched = [p for p in parts if p in snapshot]
            if not matched:
                log.warning("目标「%s」本轮未匹配到任何 SKU（Apple 接口可能暂未返回），"
                            "暂不推送，下轮重试", t.get("name") or tid)
                continue
            title, body = notifier.format_snapshot(snapshot, parts, sku_meta, stores)
            ok, detail = notifier.send(t.get("bark_url"), title, body)
            log.info("目标「%s」首次全量推送：%s", t.get("name") or tid, "成功" if ok else f"失败 {detail}")
            if ok:  # 失败则不落标记，下一轮重试全量推送
                inited[tid] = {"since": now, "parts": list(parts)}
                touched_init = True
            continue

        # 已初始化：拆成「已有 SKU 的变化」+「新增 SKU 的当前状态」
        seen = set((inited.get(tid) or {}).get("parts") or [])
        new_parts = [p for p in parts if p not in seen]
        existing = [p for p in parts if p in seen]

        # edge A：新增 SKU → 只推该 SKU 的当前状态（含无货），不推已有 SKU
        if new_parts:
            new_entries = state_mod.initial_snapshot(snapshot, new_parts)
            results = notifier.push_target(t, new_entries, sku_meta)
            ok = sum(1 for r in results if r[0])
            log.info("目标「%s」新增 SKU 首推 %d 条，成功 %d",
                     t.get("name") or tid, len(results), ok)
            for r in results:
                if not r[0]:
                    log.error("推送失败：%s", r[1])
            # 无实际推送（门店过滤后为空）或全部成功才记为已见；否则下一轮重试
            if not results or (ok == len(results)):
                inited[tid] = {"since": (inited.get(tid) or {}).get("since") or now,
                               "parts": list(parts)}
                touched_init = True

        # 已有 SKU：只推库存变动
        if existing:
            changes = state_mod.diff(prev, snapshot, existing, t.get("notify_on") or ["available"])
            if not changes:
                continue
            results = notifier.push_target(t, changes, sku_meta)
            ok = sum(1 for r in results if r[0])
            log.info("目标「%s」推送 %d 条，成功 %d", t.get("name") or tid, len(results), ok)
            for r in results:
                if not r[0]:
                    log.error("推送失败：%s", r[1])

    if touched_init:
        state_mod.save_init(init_path, inited)
    state_mod.save(state_path, state_mod.apply(prev, snapshot))
    return snapshot, failed


def _install_signal_handler():
    def handler(signum, frame):
        global _SHUTDOWN
        _SHUTDOWN = True
        log.info("收到信号 %s，本轮结束后退出", signum)

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, handler)
        except Exception:
            pass


def loop(data_dir=DEFAULT_DATA_DIR, skus_path="skus_hk.json", once=False):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _install_signal_handler()

    cfg_path = os.path.join(data_dir, "config.json")
    state_path = os.path.join(data_dir, "state.json")
    init_path = os.path.join(data_dir, "init.json")  # 已完成首轮全量推送的 target 标记
    profile = os.path.join(data_dir, "profile")
    sku_meta = load_sku_meta(skus_path)

    while True:
        cfg = config_mod.load_cached(cfg_path)
        try:
            snapshot, failed = run_once(cfg, sku_meta, profile, state_path, init_path)
            log.info("本轮完成：%d 个 SKU，失败批次 %d", len(snapshot), len(failed))
        except Exception as e:  # 单轮异常不能让常驻进程死掉
            log.exception("本轮异常：%s", e)

        if once or _SHUTDOWN:
            return 0
        time.sleep(cfg.get("interval_sec", 120) + random.uniform(0, config_mod.RANDOM_JITTER_MAX))


if __name__ == "__main__":
    sys.exit(loop(once="--once" in sys.argv))
