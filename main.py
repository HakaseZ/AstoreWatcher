#!/usr/bin/env python3
"""CLI 入口：poll（常驻轮询）/ once（单次查询打印）/ web（配置界面）。"""

import argparse
import json
import os
import sys

from browser import BrowserSession


def cmd_once(args):
    """单次查询并打印（M1 行为，便于排障）。"""
    with open(args.skus, encoding="utf-8") as f:
        skus = json.load(f)
    parts = [s["part"] for s in skus]
    meta = {s["part"]: s for s in skus}

    session = BrowserSession(args.profile, location=args.location)
    try:
        session.start()
        snapshot, failed = session.fetch_all(parts)
        if failed and not snapshot:  # profile 被污染 → 重建重试
            session.close()
            session.rebuild_profile()
            session.start()
            snapshot, failed = session.fetch_all(parts)
    finally:
        session.close()

    stores = []
    for per_store in snapshot.values():
        for name in per_store:
            if name not in stores:
                stores.append(name)

    print(f"地点：{args.location}　门店数：{len(stores)}　SKU：{len(parts)}")
    for part in parts:
        m = meta[part]
        label = f"{m['model']} {m['capacity']} {m['color']}"
        per_store = snapshot.get(part) or {}
        print(f"\n{label} ({part})")
        if not per_store:
            print("  未取到数据")
            continue
        picks = [n for n, v in per_store.items() if v.get("display") == "available"]
        for name in stores:
            entry = per_store.get(name) or {}
            mark = "✅" if entry.get("display") == "available" else "  "
            print(f"  {mark} {name:<18} {entry.get('quote') or entry.get('display') or '?'}")
        if picks:
            print(f"  → 可取貨：{', '.join(picks)}")

    if failed:
        print("\n以下批次查询失败：", file=sys.stderr)
        for f_ in failed:
            print(f"  HTTP {f_['status']}：{f_['parts'][0]} … {f_['parts'][-1]}", file=sys.stderr)
        return 1
    return 0


def cmd_poll(args):
    import poller

    return poller.loop(data_dir=args.data_dir, skus_path=args.skus,
                       once=args.once)


def cmd_web(args):
    import uvicorn
    from web import app

    uvicorn.run(app, host=args.host, port=args.port)


def main():
    ap = argparse.ArgumentParser(prog="main.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("once", help="单次查询全部 SKU 并打印")
    p.add_argument("--skus", default="skus_hk.json")
    p.add_argument("--location", default=os.environ.get("LOCATION", "中環"))
    p.add_argument("--profile", default=os.path.join(
        os.environ.get("DATA_DIR", "/data"), "profile"))
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("poll", help="常驻轮询 + 推送")
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    p.add_argument("--skus", default="skus_hk.json")
    p.add_argument("--once", action="store_true", help="只跑一轮就退出")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("web", help="启动配置界面")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    p.set_defaults(func=cmd_web)

    args = ap.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
