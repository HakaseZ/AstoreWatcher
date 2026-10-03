#!/usr/bin/env python3
"""Bark 推送（M2）。

- bark_url 由用户在界面里填完整 URL，**原样 POST**，自托管 Bark 同样适用
- 只用标准库 urllib（项目不引入 requests）
- 任何异常都在 send 内部吃掉并返回 `(False, 错误信息)`，推送失败不能拖垮轮询
"""

import json
import urllib.error
import urllib.request

import config as config_mod

BARK_TIMEOUT = 10

# 所有推送统一带的 Apple 图标（Bark icon 参数，iOS 15+）。
# 必须用 PNG/JPG：Apple 主页 favicon.ico 是 image/x-icon，iOS 通知不渲染 .ico，
# 故改用 Apple 自家的 apple-touch-icon.png（180x180 PNG）。
APPLE_ICON = "https://www.apple.com/apple-touch-icon.png"
# 目标未填 order_url 时的兜底跳转地址（点横幅落地页）
APPLE_HOME = "https://www.apple.com"

# 一条推送里最多列出多少条变化，超出的末尾补「等 N 條」
MAX_LISTED = 5

# 响应/错误信息最多回传多少字符，避免日志被灌爆
DETAIL_LIMIT = 200


def checked_line(checked_at):
    """推送正文末尾的检查时间行。

    语义是**取数时刻**而不是推送时刻：同一份 snapshot 会分发给多个目标，各次推送
    之间相隔十几秒，但它们的检查时刻必须是同一个（该时刻由 worker 在取数完成时算出）。
    """
    return f"檢查時間：{checked_at}" if checked_at else ""


def with_checked_at(body, checked_at):
    """把检查时间追加到正文末尾。

    刻意放在**发送层**而不是 format_snapshot / build_messages 那些纯文案函数里：
    纯文案函数保持纯净可断言（tests/test_notifier.py 里的严格相等断言都打在它们身上），
    时间戳只在实际要发出去的那一刻才拼上去。
    """
    line = checked_line(checked_at)
    if not line:
        return body or ""
    base = body or ""
    return f"{base}\n{line}" if base else line


def send(bark_url, title, body, *, group="AstoreWatcher", icon=None, level=None,
         is_archive=None, url=None, checked_at=None):
    """POST JSON 到 Bark，返回 (是否成功, 响应文本片段或错误信息)。

    payload 只包含非空字段：title / body / group / icon / level / isArchive / url。
    url 为点击通知横幅跳转的地址（无货→有货时带下单页）。
    checked_at 为本次数据的检查时刻（"%Y-%m-%d %H:%M"），追加到正文末尾。
    """
    payload = {"title": title or "", "body": with_checked_at(body, checked_at)}
    if group:
        payload["group"] = group
    if icon:
        payload["icon"] = icon
    if level:
        payload["level"] = level
    if is_archive is not None:  # Bark 收 1/0；False 也要明确发出去
        payload["isArchive"] = 1 if is_archive else 0
    if url:
        payload["url"] = url

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            bark_url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=BARK_TIMEOUT) as resp:
            raw = resp.read()
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return True, text[:DETAIL_LIMIT]
    except urllib.error.HTTPError as e:  # Bark 4xx/5xx 会带一段 JSON 说明
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        detail = detail[:DETAIL_LIMIT]
        return False, f"HTTP {e.code}" + (f"：{detail}" if detail else "")
    except Exception as e:  # URLError / 空 URL / 编码问题 … 一律不外抛
        return False, f"{type(e).__name__}: {e}"


def _sku_label(part, sku_meta):
    """拼出「iPhone 18 Pro Max 512GB 布根地紅色」；元数据缺失则退回 part。"""
    meta = (sku_meta or {}).get(part) or {}
    label = " ".join(str(meta[k]) for k in ("model", "capacity", "color") if meta.get(k))
    return label or part


def format_change(change, sku_meta):
    """把 state.diff / initial_snapshot 返回的一条变化转成推送文案，返回 (title, body)。

    变动推送标题统一为「您关注的 {型号全称} 监测到库存变化」。
    """
    part = change.get("part") or ""
    store = change.get("store") or ""
    to = change.get("to") or ""
    quote = change.get("quote") or ""

    label = _sku_label(part, sku_meta)
    title = f"您关注的 {label} 监测到库存变化"
    if not quote:
        quote = "可取貨" if to == "available" else "暫無供應"
    body = f"門店：{config_mod.to_display(store)}\n狀態：{quote}"
    return title, body


def _model_of(part, sku_meta):
    """取 part 的型号名；缺失时归到「其他」。"""
    return ((sku_meta or {}).get(part) or {}).get("model") or "其他"


def _change_line(change, sku_meta):
    """一条变化的正文：含型号全称 + 门店 + 状态。

    保留给 format_change 这类「一次只推一条」的场景；merged 模式下的多条编排
    走 _sku_block（会把同一 SKU 的多家门店合并进同一行）。
    """
    store = change.get("store") or ""
    to = change.get("to") or ""
    quote = change.get("quote") or ""
    if not quote:
        quote = "可取貨" if to == "available" else "暫無供應"
    return f"{_sku_label(change.get('part'), sku_meta)}\n門店：{config_mod.to_display(store)}\n狀態：{quote}"


def _sku_block(part, items, sku_meta):
    """一个 SKU 的变化块：型号一行 + 有货门店一行 + 无货门店一行。

    同一 SKU 在多家门店的变化合并进同一行，而不是每家门店重复一遍型号名 ——
    三家店同时补货原本要 9 行，现在 2 行就看得完。
    """
    lines = [_sku_label(part, sku_meta)]
    for to, label in (("available", "可取貨"), ("unavailable", "暫無供應")):
        # 带上每条变化的 quote（如「備妥於： 今日」，来自 Apple 的 pickupSearchQuote，
        # 由 state.diff 透传过来）：丢掉它就只剩「可取貨」和店名，看不出什么时候能取。
        stores = [config_mod.to_display(c.get("store"))
                  + (f"（{c.get('quote')}）" if c.get("quote") else "")
                  for c in items if c.get("to") == to]
        if stores:
            lines.append(f"{label}：{'、'.join(stores)}")
    return "\n".join(lines)


def _merged_messages(changes, sku_meta, order_url):
    """merged 模式：按型号分组，每个型号一条推送（多型号 → 多条）。

    - 同一 SKU 的多家门店合并到一行（见 _sku_block），多个 SKU 各占一段
    - 某型号下只有一个 SKU 变动 → 标题用完整型号（型号+颜色+容量）
    - 某型号下多个 SKU 变动 → 标题用型号名
    - 每条最多列 MAX_LISTED 个 SKU，超出补「等 N 條」
    - 该型号分组内有任意「有货」变动时，附 order_url（点横幅去下单）
    """
    by_model = {}
    order = []
    for c in changes:
        m = _model_of(c.get("part"), sku_meta)
        if m not in by_model:
            by_model[m] = []
            order.append(m)
        by_model[m].append(c)

    messages = []
    for m in order:
        items = by_model[m]
        # 按 SKU 聚合：一个 SKU 一段，段内把它的各家门店合并成一行
        parts = []
        for c in items:
            p = c.get("part")
            if p not in parts:
                parts.append(p)
        listed = parts[:MAX_LISTED]
        if len(parts) == 1:
            title = "您关注的 " + _sku_label(parts[0], sku_meta) + " 监测到库存变化"
        else:
            title = f"您关注的 {m} 监测到库存变化"
        body = "\n\n".join(_sku_block(p, [c for c in items if c.get("part") == p], sku_meta)
                           for p in listed)
        if len(parts) > len(listed):
            body += f"\n\n等 {len(parts) - len(listed)} 條"
        url = order_url if any(c.get("to") == "available" for c in items) else None
        messages.append((title, body, url))
    return messages


def _per_store_messages(changes, sku_meta, order_url):
    """per_store 模式：按「门店 + 方向」分条，一家店最多两条（有貨了 / 無貨了）。

    同一家店同时有补货和售罄时**必须拆成两条**：混在一条「有貨了」里、正文却夹着
    「暫無供應」的 SKU，读的人根本不知道到底能不能去买。拆分后每条只含同一方向的
    SKU，标题与正文一致。有货那条才带 order_url。
    """
    stores = []
    by_store = {}
    for c in changes:
        store = c.get("store") or ""
        if store not in by_store:
            by_store[store] = {}
            stores.append(store)  # 保持门店出现顺序
        to = "available" if c.get("to") == "available" else "unavailable"
        by_store[store].setdefault(to, []).append(c)

    messages = []
    for store in stores:
        for to in ("available", "unavailable"):  # 同一家店的两条相邻，有货在前
            items = (by_store[store].get(to) or [])
            if not items:
                continue
            title = (f"{config_mod.to_display(store)} 有貨了"
                     if to == "available" else f"{config_mod.to_display(store)} 無貨了")
            lines = [f"{_sku_label(c.get('part') or '', sku_meta)}："
                     f"{c.get('quote') or ('可取貨' if to == 'available' else '暫無供應')}"
                     for c in items]
            url = order_url if to == "available" else None
            messages.append((title, "\n".join(lines), url))
    return messages


def build_messages(target, changes, sku_meta):
    """按 target["push_mode"] 把一组变化编排成待发消息，返回 [(title, body, url), ...]。

    - merged（默认）：全部合并成一条，最多列 MAX_LISTED 条，其余补「等 N 條」
    - per_store：按门店分条，每条列该门店所有变化的 SKU
    - url：该分组含「有货」变动时为 target 的 order_url（缺省回退 Apple 主页），否则 None

    纯函数，不发网络请求，方便单独测文案。
    """
    mode = (target or {}).get("push_mode")
    order_url = (target or {}).get("order_url") or APPLE_HOME
    if mode == "per_store":
        return _per_store_messages(changes, sku_meta, order_url)
    return _merged_messages(changes, sku_meta, order_url)


def format_snapshot(snapshot, parts, sku_meta, stores=None):
    """首次全量快照文案，返回 (title, body)。

    每个 SKU 一行：`{型号}：{N} 店可取貨：{店名}`（有货时**一律列出全部店名** ——
    只说「N 店可取货」不知道该去哪家买）；全无货写「{N} 店暫無供應」；
    没取到数据写「未取到數據」。

    stores 为勾选的门店名列表（来自 target["stores"]）；为空表示全部门店。
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if parts:
        part_keys = [p for p in parts if p in snapshot]
    else:
        part_keys = list(snapshot.keys())

    # 只保留目标勾选的门店；留空表示全部门店。目标里存的是中文店名，
    # 需归一到 Apple 接口返回的英文 slug 才能对上 snapshot 的键。
    if stores:
        wanted = {config_mod.to_key(s) for s in stores}
        snapshot = {p: {s: e for s, e in (snapshot.get(p) or {}).items() if s in wanted}
                    for p in part_keys}
        # 用 `p in snapshot` 而不是 `snapshot.get(p)`：后者会把「本轮没查到任何门店
        # 数据」的 part 一并剔掉，于是那个 SKU 在推送里整行消失，用户分不清它是
        # 「没货」还是「压根没查到」。门店已是必填，生产上永远走这条过滤分支。
        # 保留它，让它落到下面的「未取到數據」。
        part_keys = [p for p in part_keys if p in snapshot]

    title = "正在为您监测库存，当前库存如下"
    lines = []
    for part in part_keys:
        label = _sku_label(part, sku_meta)
        store_map = snapshot.get(part)
        if not isinstance(store_map, dict) or not store_map:
            lines.append(f"{label}：未取到數據")
            continue

        available = []
        for store, entry in store_map.items():
            display = entry.get("display") if isinstance(entry, dict) else entry
            if display == "available":
                available.append(config_mod.to_display(store))

        if not available:
            lines.append(f"{label}：{len(store_map)} 店暫無供應")
        else:
            # 有货就把店名全列出来（门店上限 6 家，不设 MAX_LISTED）：
            # 「N 店可取货」这句话对「该去哪家买」没有任何帮助。
            lines.append(f"{label}：{len(available)} 店可取貨：{'、'.join(available)}")

    return title, "\n".join(lines)


def snapshot_has_available(snapshot, parts, stores=None):
    """判断全量快照里、目标关注的 parts/stores 范围内是否有任意「有货」门店。

    用于首推（首次全量 / 重新启用）决定是否带下单链接：全无货时不应带，
    与变动推送「有货才带链接」的规则保持一致。
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    part_keys = [p for p in parts if p in snapshot] if parts else list(snapshot)
    wanted = {config_mod.to_key(s) for s in stores} if stores else None
    for part in part_keys:
        store_map = snapshot.get(part)
        if not isinstance(store_map, dict):
            continue
        for store, entry in store_map.items():
            if wanted is not None and store not in wanted:
                continue
            display = entry.get("display") if isinstance(entry, dict) else entry
            if display == "available":
                return True
    return False


def push_target(target, changes, sku_meta, checked_at=None):
    """给一个目标推送一组变化，返回每条推送的 (ok, detail)。

    - target["enabled"] 为 False → 直接返回 []
    - 只推该目标 parts 内的变化（parts 为空表示全部）
    - 条数取决于 push_mode：merged 一条，per_store 每家门店一条
    - checked_at 透传给每条推送（同一轮的不同目标共用同一个检查时刻）
    """
    if not target or not target.get("enabled"):
        return []
    if not changes:
        return []

    # SKU 与门店均为必填：空表示配置不完整，不再当作「全部」推送
    parts = target.get("parts") or []
    if not parts:
        return []
    stores = target.get("stores") or []
    if not stores:
        return []

    # 只推该目标关注的 SKU（防御性：调用方传错时也不该把别人的 SKU 推出去）
    wanted = set(parts)
    changes = [c for c in changes if c.get("part") in wanted]

    # 只推该目标勾选的门店（目标里存中文店名，归一到英文 slug 再匹配）
    wanted_stores = {config_mod.to_key(s) for s in stores}
    changes = [c for c in changes if c.get("store") in wanted_stores]

    if not changes:
        return []

    return [send(target.get("bark_url"), title, body, icon=APPLE_ICON, url=url,
                 checked_at=checked_at)
            for title, body, url in build_messages(target, changes, sku_meta)]
