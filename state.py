#!/usr/bin/env python3
"""状态去重：data/state.json（M2）。

两种结构的区别：
- **state**（持久化，落盘）：`{part: {store: "available"|"unavailable"}}`
- **snapshot**（每轮查询结果，内存）：`{part: {store: {"display": ..., "quote": ...}}}`

对比时只报“变化”，且首轮（prev 里没有该键）只在 available 时报 —— 否则第一次
启动会把 32 个 SKU × 6 家门店全推一遍。

**首次全量推送**由调用方（poller）另走一条路：先用 `initial_snapshot` 取出该目标
关注的全量条目（含 unavailable）推一次，并把 target id 记进 `init.json`；
此后 prev 里已有记录，回到 `diff` 的变化推送。`init.json` 单独一个文件，
不混进 state.json 的结构里。
"""

import json
import os
import tempfile

AVAILABLE = "available"
UNAVAILABLE = "unavailable"


def _backup_bad(path):
    """把坏文件改名留档为 <path>.bad。失败也不抛出。"""
    try:
        os.replace(os.fspath(path), os.fspath(path) + ".bad")
    except OSError:
        pass


def load(path):
    """读状态。文件不存在 / 损坏 → 返回 {}（损坏时备份为 .bad）。不抛异常。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _backup_bad(path)
        return {}
    return data if isinstance(data, dict) else {}


def save(path, state):
    """原子写状态：同目录临时文件 → fsync → os.replace。目录不存在先创建。"""
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    data = json.dumps(state, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
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


def load_init(path):
    """读「已做过首次全量推送」的标记：`{target_id: "ISO 时间字符串"}`。

    文件不存在 / 损坏 → 返回 {}（损坏时备份为 .bad）。不抛异常。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _backup_bad(path)
        return {}
    return data if isinstance(data, dict) else {}


def save_init(path, mapping):
    """原子写首次推送标记（复用 save 的原子写实现）。"""
    save(path, mapping if isinstance(mapping, dict) else {})


def prune_init(path, cfg):
    """摘除所有未启用 / 已删除目标的首推标记。

    首推标记在 Watcher 首次全量推送后写入；重新启用目标时要再推一次全量，标记必须在
    禁用发生的这一刻就清掉（后端收得到禁用请求），否则若 Watcher 没在禁用期间跑过一轮，
    标记残留，重新启用会被误判为已初始化而不再推全量。
    """
    try:
        inited = load_init(path)
    except OSError:
        return
    enabled_ids = {t.get("id") for t in (cfg.get("targets") or []) if t.get("enabled", True)}
    changed = False
    for tid in list(inited):
        if tid not in enabled_ids:
            inited.pop(tid, None)
            changed = True
    if changed:
        try:
            save_init(path, inited)
        except OSError:
            pass


def _display_of(entry):
    """从 snapshot 的单条目里取出 (display, quote)；结构异常返回 None。"""
    if isinstance(entry, dict):
        to = AVAILABLE if entry.get("display") == AVAILABLE else UNAVAILABLE
        quote = entry.get("quote")
        return to, (quote if isinstance(quote, str) else "")
    if isinstance(entry, str):  # 容错：直接给了 display 字符串
        return (AVAILABLE if entry == AVAILABLE else UNAVAILABLE), ""
    return None


def diff(prev_state, snapshot, parts, notify_on):
    """比对上一轮 state 与本轮 snapshot，返回需要通知的变化列表。

    - parts 只关心的 part（空/None 表示全都要）；part 不在本轮 snapshot 里则跳过
    - to == available   且 "available"   in notify_on 且 prev 不是 available → 记变化
    - to == unavailable 且 "unavailable" in notify_on 且 prev 是 available   → 记变化
    - prev 缺该键（首次运行）视为 None：只在 to == available 时记，避免首轮刷屏

    返回 `[{"part", "store", "from": str|None, "to": str, "quote": str}, ...]`
    """
    prev_state = prev_state if isinstance(prev_state, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    notify_on = notify_on if isinstance(notify_on, (list, tuple)) else ["available"]
    want_available = AVAILABLE in notify_on
    want_unavailable = UNAVAILABLE in notify_on

    if parts:  # 指定了关注列表 → 按给定顺序、且只处理本轮查到的
        part_keys = [p for p in parts if p in snapshot]
    else:
        part_keys = list(snapshot.keys())

    changes = []
    for part in part_keys:
        cur = snapshot.get(part)
        if not isinstance(cur, dict):
            continue
        prev_part = prev_state.get(part)
        prev_part = prev_part if isinstance(prev_part, dict) else {}

        for store, entry in cur.items():
            parsed = _display_of(entry)
            if parsed is None:
                continue
            to, quote = parsed
            before = prev_part.get(store)  # 首次运行时为 None
            if not isinstance(before, str):
                before = None

            if to == AVAILABLE:
                if want_available and before != AVAILABLE:
                    changes.append({"part": part, "store": store,
                                    "from": before, "to": to, "quote": quote})
            else:
                # 下架只在“之前确实有货”时报，首轮（None）不报
                if want_unavailable and before == AVAILABLE:
                    changes.append({"part": part, "store": store,
                                    "from": before, "to": to, "quote": quote})

    return changes


def initial_snapshot(snapshot, parts):
    """取某目标关注范围的全量条目（**含 unavailable**），供首次加入时推送一次。

    parts 为空/None 表示全部。返回结构与 diff 的元素一致，只是 `from` 恒为 None：
    `[{"part", "store", "from": None, "to": display, "quote": quote}, ...]`
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    if parts:
        part_keys = [p for p in parts if p in snapshot]
    else:
        part_keys = list(snapshot.keys())

    entries = []
    for part in part_keys:
        cur = snapshot.get(part)
        if not isinstance(cur, dict):
            continue
        for store, entry in cur.items():
            parsed = _display_of(entry)
            if parsed is None:
                continue
            to, quote = parsed
            entries.append({"part": part, "store": store,
                            "from": None, "to": to, "quote": quote})
    return entries


def apply(prev_state, snapshot):
    """把本轮 snapshot 合并进旧 state，返回新 state（只保留 display 字符串）。

    不修改传入的 prev_state；snapshot 之外的旧条目保留。
    """
    new_state = {}
    for part, stores in (prev_state or {}).items():
        if isinstance(stores, dict):
            new_state[part] = {s: v for s, v in stores.items() if isinstance(v, str)}

    for part, stores in (snapshot or {}).items():
        if not isinstance(stores, dict):
            continue
        target = new_state.setdefault(part, {})
        for store, entry in stores.items():
            parsed = _display_of(entry)
            if parsed is None:
                continue
            target[store] = parsed[0]

    return new_state
