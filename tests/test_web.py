"""首推标记摘除逻辑的轻量单测（不依赖 FastAPI / web）。"""
import json

from state import prune_init


def test_prune_init_removes_disabled_and_missing(tmp_path):
    """禁用 / 已删除目标的首次推送标记应在写配置时立即摘除，
    使重新启用能可靠再推一次全量（不依赖 Watcher 跑过禁用周期）。"""
    init_file = tmp_path / "init.json"
    cfg = {
        "targets": [
            {"id": "a", "enabled": True},   # 启用：标记应保留
            {"id": "b", "enabled": False},  # 禁用：标记应摘除
            # "c" 不在配置里（已删除）：标记应摘除
        ]
    }
    init_file.write_text(json.dumps({
        "a": {"since": "x", "parts": []},
        "b": {"since": "x", "parts": []},
        "c": {"since": "x", "parts": []},
    }))

    prune_init(str(init_file), cfg)

    inited = json.loads(init_file.read_text())
    assert "a" in inited, "启用目标的标记不应被摘除"
    assert "b" not in inited, "禁用目标的标记应被摘除"
    assert "c" not in inited, "已删除目标的标记应被摘除"
