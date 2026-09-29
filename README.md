# AstoreWatcher

Apple Store（香港）库存监控相关的资料与数据集合。

## 内容

- `apple_hk_18_models.md` — 港版 iPhone 18 Pro / Pro Max 的型号、颜色、容量与 SKU 对照表，以及 `fulfillment-messages` 库存查询接口的调用约束（域名、Akamai JS 挑战、参数分片、返回值字段等实测结论）。
- `skus_hk.json` — 上述 32 个 SKU 的机器可读清单（`part` / `model` / `color` / `capacity`），供轮询脚本直接加载。

## 备注

- 库存接口只有取货状态（`available` / `unavailable`），没有精确台数。
- 接口受 Akamai Bot Manager 保护，纯命令行 HTTP 库会被 541 拦截，必须在真实浏览器页面上下文中请求。
