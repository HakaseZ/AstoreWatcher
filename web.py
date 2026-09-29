#!/usr/bin/env python3
"""M2b：配置界面后端 —— 只提供 JSON API，并挂载静态页面。

本进程不参与任何浏览器 / 轮询逻辑（那部分由常驻的 watcher 进程负责，见 PLAN.md M2）。
watcher 通过 mtime 热加载同一个 config.json，所以界面改完下一轮即生效，无需重启。

**规范化逻辑只有一份**：数值夹取、parts 过滤、notify_on / push_mode 兜底、id 生成等全部由
`config.validate()` 负责；本模块只在写盘前补一步「用户侧校验」（空 Bark 地址报错、空名称补默认值），
然后把 config.validate 的返回值落盘并回显。因此界面 GET 看到的内容与 watcher 实际使用的一致。
刻意不对 config.py 做 import 降级 —— 缺了就该报错暴露，而不是静默回退到另一套实现。

环境变量：
  CONFIG_PATH  config.json 路径，默认 /data/config.json（必须与 watcher 使用的
                路径一致，否则界面保存的配置轮询进程读不到）。不存在时不回退，
                直接在此路径创建；读不到则返回空配置由前端处理。
  SKUS_PATH    SKU 元数据文件路径，默认依次尝试 ./skus_hk.json、
               本文件同目录下的 skus_hk.json、/app/skus_hk.json（容器内路径）

启动：uvicorn web:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config as config_mod
from fastapi import Body, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------- 常量与路径

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# config.validate 不补默认名称（空字符串会保留），所以要在交给它之前填好
DEFAULT_TARGET_NAME = "未命名目标"
DEFAULT_PUSH_MODE = "merged"      # 请求模型的默认值；实际兜底由 config.validate 做
BARK_GROUP = "AstoreWatcher"
BARK_TIMEOUT = 10                 # 秒

HK_TZ = timezone(timedelta(hours=8))


def config_path() -> str:
    """config.json 的实际路径：CONFIG_PATH 环境变量指定，默认 /data/config.json。

    必须与 watcher（poller.DEFAULT_DATA_DIR）使用的路径一致，否则界面写入的配置
    轮询进程读取不到。不做「文件不存在就回退到本目录」的退让——那正是把配置写到
    容器文件系统、导致两边不一致的旧 bug 来源。
    """
    return os.environ.get("CONFIG_PATH") or "/data/config.json"


def skus_path() -> str:
    """SKU 元数据路径：SKUS_PATH > 当前目录 > 本文件目录 > /app（容器内）。"""
    candidates = []
    env = os.environ.get("SKUS_PATH")
    if env:
        candidates.append(env)
    else:
        candidates.append(os.path.join(os.getcwd(), "skus_hk.json"))
        candidates.append(os.path.join(BASE_DIR, "skus_hk.json"))
        candidates.append("/app/skus_hk.json")
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


# ---------------------------------------------------------------- 基础读写

def load_skus() -> list[dict]:
    """读取 SKU 元数据。文件缺失/损坏返回空列表（由调用方决定如何报错）。"""
    path = skus_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict) and row.get("part")]


def known_parts() -> set[str]:
    return {str(row["part"]) for row in load_skus()}


# ---------------------------------------------------------------- 读 / 写配置

def read_config() -> dict:
    """读配置（**非破坏性**）：不存在→默认值；损坏→返回默认值，但不改名备份。

    与 watcher 的 config.load 唯一的差别就是不备份：一次页面刷新不该把用户
    的坏配置悄悄挪走，备份只在 watcher 读 / 写路径上做。
    规范化仍然复用 config.validate，保证与 watcher 看到的一致。
    """
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):  # FileNotFoundError 属于 OSError
        return config_mod.default_config()
    return config_mod.validate(data)


def prepare_targets(raw_targets) -> list[dict]:
    """用户侧校验，跑在 config.validate 之前。

    为什么要抢在前面：validate 遇到空 bark_url 会**静默丢弃**该目标，用户会以为保存成功；
    而空名称它也会原样保留。这两件都该由界面明确告知。
    """
    if not isinstance(raw_targets, list):
        raise ValueError("targets 必须是对象数组")
    prepared = []
    for item in raw_targets:
        if not isinstance(item, dict):
            raise ValueError("targets 必须是对象数组")
        item = dict(item)
        name = str(item.get("name") or "").strip() or DEFAULT_TARGET_NAME
        if not str(item.get("bark_url") or "").strip():
            raise ValueError(f"目标「{name}」的 Bark 地址不能为空")
        item["name"] = name
        prepared.append(item)
    return prepared


def prepare_config(raw: dict) -> dict:
    """用户侧校验 → config.validate（权威规范化），返回可落盘、可回显的配置。"""
    data = copy.deepcopy(raw)
    data["targets"] = prepare_targets(data.get("targets"))
    return config_mod.validate(data, known_parts())


def write_config(data: dict) -> dict:
    """校验 → 规范化 → 原子写（config.save），返回 validate 的结果而非入参。"""
    config = prepare_config(data)
    config_mod.save(config_path(), config)
    return config


def commit(data: dict) -> dict:
    """写盘并把异常翻译成 HTTP 状态码：校验类 400，写入失败 500。"""
    try:
        return write_config(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入 config.json 失败：{exc}")


# ---------------------------------------------------------------- 请求模型

class TargetIn(BaseModel):
    name: str = ""
    bark_url: str = ""
    enabled: bool = True
    parts: list[str] = []
    stores: list[str] = []
    notify_on: list[str] = ["available"]
    push_mode: str = DEFAULT_PUSH_MODE


class ConfigIn(BaseModel):
    location: str = config_mod.DEFAULT_LOCATION
    interval_sec: int = config_mod.DEFAULT_INTERVAL_SEC
    targets: list[TargetIn] = []


# ---------------------------------------------------------------- Bark 推送

def bark_post(url: str, title: str, body: str) -> tuple[bool, str]:
    """按 Bark 协议 POST JSON。返回 (是否成功, 详情文本)。

    只用标准库 urllib；失败不抛异常，详情交给界面展示。
    """
    payload = json.dumps(
        {"title": title, "body": body, "group": BARK_GROUP},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AstoreWatcher/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=BARK_TIMEOUT) as resp:
            text = resp.read(2000).decode("utf-8", "replace")
            status = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= status < 300
            return ok, f"HTTP {status}：{text[:300]}"
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(2000).decode("utf-8", "replace")
        except Exception:
            text = ""
        return False, f"HTTP {exc.code}：{text[:300] or exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"请求失败：{exc.reason}"
    except Exception as exc:                      # 超时、SSL 错误等
        return False, f"请求失败：{type(exc).__name__}：{exc}"


# ---------------------------------------------------------------- API

app = FastAPI(title="AstoreWatcher 配置界面", version="0.1")


@app.get("/api/config")
def api_get_config() -> dict:
    """返回完整配置；文件不存在时返回带默认值的配置。

    与 watcher 读同一个文件、同一套 validate，界面看到的就是实际生效的配置。
    """
    return read_config()


@app.put("/api/config")
def api_put_config(body: ConfigIn = Body(...)) -> dict:
    """整体保存配置（用户侧校验 → validate → 原子写），返回保存后的配置。"""
    return commit(body.model_dump())


@app.get("/api/skus")
def api_get_skus() -> list[dict]:
    """返回 skus_hk.json 原样的 SKU 数组（32 条）。"""
    skus = load_skus()
    if not skus:
        raise HTTPException(
            status_code=500,
            detail=f"SKU 元数据不可用：{skus_path()}",
        )
    return skus


def known_stores() -> list[str]:
    """「推送门店」选项的门店名列表。

    优先用轮询时从 Apple 接口发现的真实店名（data/stores.json），
    缺失或为空时回退到 config.HK_STORES（6 家 HK 门店中文名）。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(config_path())), "stores.json")
    try:
        with open(path, encoding="utf-8") as f:
            discovered = json.load(f)
    except (OSError, ValueError):
        discovered = []
    merged = [s for s in discovered if isinstance(s, str)] if isinstance(discovered, list) else []
    for s in config_mod.HK_STORES:
        if s not in merged:
            merged.append(s)
    return merged


@app.get("/api/stores")
def api_get_stores() -> list[str]:
    """返回可选门店名列表（中文），供「新增目标」里勾选推送门店。"""
    return known_stores()


@app.post("/api/targets")
def api_create_target(body: TargetIn) -> dict:
    """新增一个推送目标，id 由 config.validate 生成。"""
    config = read_config()
    known_ids = {t["id"] for t in config["targets"]}
    data = dict(config)
    data["targets"] = list(config["targets"]) + [body.model_dump()]
    saved = commit(data)
    # 原有目标的 id 不会变，所以多出来的那个就是新建的
    fresh = [t for t in saved["targets"] if t["id"] not in known_ids]
    return fresh[0]


@app.put("/api/targets/{target_id}")
def api_update_target(target_id: str, body: TargetIn) -> dict:
    """更新指定目标（保留原 id）。"""
    config = read_config()
    index = next((i for i, t in enumerate(config["targets"]) if t["id"] == target_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail=f"找不到目标 {target_id}")

    data = dict(config)
    targets = list(config["targets"])
    targets[index] = dict(body.model_dump(), id=target_id)
    data["targets"] = targets
    saved = commit(data)
    return next(t for t in saved["targets"] if t["id"] == target_id)


@app.delete("/api/targets/{target_id}")
def api_delete_target(target_id: str) -> list[dict]:
    """删除目标，返回剩余的 targets。"""
    config = read_config()
    remaining = [t for t in config["targets"] if t["id"] != target_id]
    if len(remaining) == len(config["targets"]):
        raise HTTPException(status_code=404, detail=f"找不到目标 {target_id}")
    data = dict(config)
    data["targets"] = remaining
    return commit(data)["targets"]


@app.post("/api/targets/{target_id}/test")
def api_test_target(target_id: str) -> dict:
    """发一条测试推送。HTTP 恒为 200，成败放在 body 里便于界面展示。"""
    config = read_config()
    target = next((t for t in config["targets"] if t["id"] == target_id), None)
    if target is None:
        return {"ok": False, "detail": f"找不到目标 {target_id}，请先保存配置"}

    now = datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    title = "AstoreWatcher 测试推送"
    body = f"目标：{target['name']}\n时间：{now}（香港时间）"
    ok, detail = bark_post(target["bark_url"], title, body)
    return {"ok": ok, "detail": detail}


# 静态页面挂载放最后，避免抢掉 /api 路由
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # 默认只绑本机回环，避免 Bark key 对外暴露
    uvicorn.run(app, host="127.0.0.1", port=8787)
