#!/usr/bin/env python3
"""配置读写：data/config.json（M2）。

设计要点：
- **绝不抛异常**：文件不存在 / JSON 损坏都回退到默认配置，损坏时把坏文件改名
  `<path>.bad` 留档（常驻进程不能被配置问题搞挂）。
- **原子写**：同目录临时文件 + `os.replace`，避免 web 界面与轮询进程同时写
  导致文件半截（读到一个截了一半的 JSON）。
- **热加载**：`load_cached` 按 mtime 判断是否需要重读，轮询每轮调用一次，
  界面改完下一轮即生效，无需重启。
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# 展示与推送一律用香港时区。worker 子进程只依赖 config，故常量放在这里，
# 供 poller / worker / notifier / web 共用同一份定义（web 侧改为 re-export）。
HK_TZ = timezone(timedelta(hours=8))


def hk_now(fmt="%Y-%m-%d %H:%M"):
    """返回香港时区当前时间的字符串，默认精确到分钟（如 '2026-10-01 17:32'）。"""
    return datetime.now(HK_TZ).strftime(fmt)


# 轮询地点：实测 location 只影响门店排序，不影响门店集合，写死中環即可
DEFAULT_LOCATION = "中環"
# 轮询间隔下限（秒）：低于此值会被夹到 30，防止请求过密触发风控
MIN_INTERVAL_SEC = 30
DEFAULT_INTERVAL_SEC = 120

# 每轮实际间隔 = interval_sec + 0~RANDOM_JITTER_MAX 的随机抖动（避免请求过于规律被风控）。
# 抖动是代码内固定的随机值，不暴露给用户配置。
RANDOM_JITTER_MAX = 10

# 香港 6 家 Apple Store 官方中文店名（取自 Apple HK 官网）。
# 轮询时 Apple 库存接口返回的 storeName 是**英文 slug**，仅用作内部与接口数据对齐的键；
# 用户可见处（界面「推送门店」选项、Bark 推送文案）一律用下表的中文名。
HK_STORES = [
    "Apple 中環",
    "Apple 廣東道",
    "Apple 銅鑼灣",
    "Apple 九龍塘",
    "Apple 觀塘",
    "Apple 沙田",
]

# Apple HK 库存接口返回的英文 slug → 官方中文店名（唯一映射源）。
STORE_DISPLAY = {
    "ifc mall": "Apple 中環",
    "Canton Road": "Apple 廣東道",
    "Causeway Bay": "Apple 銅鑼灣",
    "Festival Walk": "Apple 九龍塘",
    "apm Hong Kong": "Apple 觀塘",
    "New Town Plaza": "Apple 沙田",
}
# 反向：中文店名 → 英文 slug（用于把界面勾选的中文归一到接口键）
STORE_BY_ZH = {zh: en for en, zh in STORE_DISPLAY.items()}


def to_display(name):
    """门店名 → 展示用中文名。已是中文 / 未收录则原样返回。"""
    if not isinstance(name, str):
        return name
    return STORE_DISPLAY.get(name, name)


def to_key(name):
    """门店名 → 与 Apple 接口数据对齐的英文 slug。已是英文 / 未收录则原样返回。"""
    if not isinstance(name, str):
        return name
    return STORE_BY_ZH.get(name, name)

# 合法的通知时机；顺序即规范化后的输出顺序
VALID_NOTIFY_ON = ("available", "unavailable")

# 推送方式：merged=所有变化合并成一条；per_store=按门店分条
VALID_PUSH_MODES = ("merged", "per_store")
DEFAULT_PUSH_MODE = "merged"


def default_config():
    """返回一份全新默认配置（目标列表为空）。"""
    return {
        "location": DEFAULT_LOCATION,
        "interval_sec": DEFAULT_INTERVAL_SEC,
        # 运维告警地址（独立 Bark URL）：Watcher 自身失联/连续失败时推送，
        # 与各推送目标的 bark_url 解耦，避免 silence failure 与运维消息混进业务推送流。
        # 留空表示不推送告警（配置页会显示提示）。
        "health_webhook": "",
        "targets": [],
    }


def _backup_bad(path):
    """把坏文件改名留档为 <path>.bad（覆盖上一份 .bad）。失败也不抛出。"""
    try:
        os.replace(os.fspath(path), os.fspath(path) + ".bad")
    except OSError:
        pass


def _clamp_int(value, default, minimum):
    """取整数并夹到 >= minimum；不可解析时回退 default。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, num)


def _normalize_url(value):
    """规范化 URL 字段：非字符串或空白 → 空串（表示未配置）。"""
    return value.strip() if isinstance(value, str) else ""


def _normalize_notify_on(raw):
    """库存翻转即推送：有货<->无货 两个方向都推，无需用户选择，始终返回两者。

    raw 参数保留仅为兼容旧配置字段，不再影响结果。
    """
    return ["available", "unavailable"]


def _normalize_push_mode(raw):
    """推送方式：非法/缺失一律回落 merged。"""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw in VALID_PUSH_MODES:
            return raw
    return DEFAULT_PUSH_MODE


def _normalize_targets(raw, valid_parts):
    """规范化目标列表：丢弃无 bark_url 的、过滤 parts、保证 id 非空唯一。"""
    if not isinstance(raw, list):
        return []

    # valid_parts 为空/None 表示不过滤（调用方还没拿到 SKU 表时）
    valid = set(valid_parts) if valid_parts else None
    targets = []
    seen_ids = set()

    for item in raw:
        if not isinstance(item, dict):
            continue

        bark_url = item.get("bark_url")
        bark_url = bark_url.strip() if isinstance(bark_url, str) else ""
        if not bark_url:  # 没有推送地址的目标没有意义，直接丢弃
            continue

        tid = item.get("id")
        tid = tid.strip() if isinstance(tid, str) else ""
        if not tid or tid in seen_ids:  # 缺失或重复 → 生成一个唯一 id
            tid = uuid.uuid4().hex[:8]
            while tid in seen_ids:
                tid = uuid.uuid4().hex[:8]
        seen_ids.add(tid)

        raw_parts = item.get("parts")
        parts = [p for p in raw_parts if isinstance(p, str)] if isinstance(raw_parts, list) else []
        if valid is not None:
            parts = [p for p in parts if p in valid]
        # 去重保序
        seen_parts = set()
        ordered_parts = []
        for p in parts:
            if p not in seen_parts:
                seen_parts.add(p)
                ordered_parts.append(p)

        # 推送门店：勾选的门店名列表；留空表示全部门店
        raw_stores = item.get("stores")
        stores = [s for s in raw_stores if isinstance(s, str)] if isinstance(raw_stores, list) else []
        seen_stores = set()
        ordered_stores = []
        for s in stores:
            if s not in seen_stores:
                seen_stores.add(s)
                ordered_stores.append(s)

        # 下单页链接（可选）：点通知横幅跳转；留空则由 notifier 回退 Apple 主页
        order_url = item.get("order_url")
        order_url = order_url.strip() if isinstance(order_url, str) else ""

        name = item.get("name")
        targets.append({
            "id": tid,
            "name": name if isinstance(name, str) else "",
            "bark_url": bark_url,
            "enabled": bool(item.get("enabled", True)),
            "parts": ordered_parts,
            "stores": ordered_stores,
            "order_url": order_url,
            "notify_on": _normalize_notify_on(item.get("notify_on")),
            "push_mode": _normalize_push_mode(item.get("push_mode")),
        })

    return targets


def validate(cfg, valid_parts=None):
    """把任意输入规范化成合法配置：补默认值、夹取数值、清洗 targets。

    valid_parts 为合法 part 集合（如 skus_hk.json 里的 32 个）；为空则不过滤。
    """
    raw = cfg if isinstance(cfg, dict) else {}

    location = raw.get("location")
    if not (isinstance(location, str) and location.strip()):
        location = DEFAULT_LOCATION

    return {
        "location": location,
        "interval_sec": _clamp_int(raw.get("interval_sec"), DEFAULT_INTERVAL_SEC, MIN_INTERVAL_SEC),
        "health_webhook": _normalize_url(raw.get("health_webhook")),
        "targets": _normalize_targets(raw.get("targets"), valid_parts),
    }


def load(path):
    """读配置。不存在 → 默认；损坏 → 备份为 .bad 后返回默认。不抛异常。"""
    try:
        with open(path, encoding="utf-8") as f:
            return validate(json.load(f))
    except FileNotFoundError:
        return default_config()
    except (OSError, ValueError):  # JSONDecodeError 属于 ValueError
        _backup_bad(path)
        return default_config()


def save(path, cfg):
    """原子写配置：同目录临时文件 → fsync → os.replace。目录不存在先创建。"""
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
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


def load_cached(path, cache={"path": None, "mtime": None, "data": None}):
    """按 mtime 热加载：无变化返回缓存对象，有变化才重新读盘。

    cache 为调用方持有的可变字典（默认参数即模块级共享缓存，够单进程用）；
    需要独立缓存时显式传入自己的 dict。
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = None  # 文件不存在：也当作一次“变化”，读到默认配置

    if cache.get("path") == path and cache.get("mtime") == mtime and cache.get("data") is not None:
        return cache["data"]

    data = load(path)
    cache["path"] = path
    cache["mtime"] = mtime
    cache["data"] = data
    return data
