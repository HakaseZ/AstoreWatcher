#!/usr/bin/env python3
"""CLI 入口：poll（常驻轮询）/ once（单次查询打印）/ web（配置界面）/ health（健康检查）。"""

import argparse
import json
import os
import sys
import time


def cmd_once(args):
    """单次查询并打印（便于排障）。

    刻意与生产走**同一条** supervisor / worker 路径：排障时若用另一套调用链，
    看到的行为就可能和生产不一致，由此得出的结论不可信。
    """
    import supervisor

    with open(args.skus, encoding="utf-8") as f:
        skus = json.load(f)
    parts = [s["part"] for s in skus]
    meta = {s["part"]: s for s in skus}

    # spec / out 临时文件与 profile 同目录，避免写进 /data 之外的地方
    data_dir = os.path.dirname(os.path.abspath(args.profile)) or "."
    result = supervisor.run_round(args.profile, parts, location=args.location,
                                  data_dir=data_dir)
    if not result.ok:
        print(f"取数失败（{result.kind or '未知'}）：{result.error or '无数据'}",
              file=sys.stderr)
    snapshot, failed = result.snapshot, result.failed

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


def cmd_health(args):
    """健康检查：按「上次成功取数」的新鲜度退出 0/1，供 docker healthcheck 用。

    只回答「这个容器还能不能干活」，不判断库存有没有变化；连续失败的告警
    由 supervisor.Health 走 Bark 负责 —— 两者职责不同，不要混。
    """
    import supervisor

    data = supervisor.load_health(args.data_dir)
    epoch = float(data.get("last_success_epoch") or 0)
    if not epoch:
        print(f"从未成功取数（last_attempt_at={data.get('last_attempt_at') or '无'}）")
        return 1
    age = int(time.time() - epoch)
    print(f"last_success_at={data.get('last_success_at')} age={age}s "
          f"consecutive_failures={data.get('consecutive_failures')} "
          f"last_error_kind={data.get('last_error_kind') or '-'}")
    return 0 if age <= args.max_age else 1


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

    p = sub.add_parser("health", help="健康检查：按最近一次成功取数时间退出 0/1")
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    p.add_argument("--max-age", type=int, default=int(os.environ.get("MAX_AGE_SEC", "900")),
                   help="上次成功取数距今多少秒内算健康（默认 900，约 3 轮轮询）")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("web", help="启动配置界面")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    p.set_defaults(func=cmd_web)

    args = ap.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
