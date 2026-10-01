# AstoreWatcher

Apple Store（香港）库存监控相关的资料与数据集合。

## 内容

- `apple_hk_18_models.md` — 港版 iPhone 18 Pro / Pro Max 的型号、颜色、容量与 SKU 对照表，以及 `fulfillment-messages` 库存查询接口的调用约束（域名、Akamai JS 挑战、参数分片、返回值字段等实测结论）。
- `skus_hk.json` — 上述 32 个 SKU 的机器可读清单（`part` / `model` / `color` / `capacity`），供轮询脚本直接加载。

## 备注

- 库存接口只有取货状态（`available` / `unavailable`），没有精确台数。
- 接口受 Akamai Bot Manager 保护，纯命令行 HTTP 库会被 541 拦截，必须在真实浏览器页面上下文中请求。

## 部署

基于 Docker（最小镜像：python:3.13-slim + apt 安装的 chromium；轮询进程与配置界面同源单镜像）。

### 1. 构建镜像

```bash
docker compose build
```

构建 `astore-watcher:latest`，同时包含常驻轮询进程（watcher）与配置界面（web）。

### 2. 启动

```bash
docker compose up -d
```

启动两个服务：

- `watcher`：常驻轮询 + 推送（`python3 main.py poll`），每 ~30 秒查一次全部已选 SKU（按 Apple 单批上限 15 个分批，批间无延迟；轮次之间叠加 0~10 秒随机抖动抗风控）。
- `web`：配置界面（`python3 main.py web`），监听 `0.0.0.0:8787`，浏览器打开 `http://<宿主机>:8787` 即可配置。

### 3. 数据目录

容器把宿主机 `./data` 挂载到容器内 `/data`，持久化以下文件：

- `config.json` — 配置（目标、Bark 地址、门店、推送方式、轮询间隔）；界面改完下一轮即生效（watcher 按 mtime 热加载）。
- `state.json` — 最近一轮从 Apple 拉到的库存快照。重新启用 / 新建目标时的「即时全量推送」就取自这里（约 2 秒，不走浏览器）。
- `init.json` — 各目标「已做过首次全量推送」的标记（禁用即摘除，确保重新启用能再推一次全量）。
- `profile/` — 浏览器会话（用于过 Apple 防护），一般无需动。取数失败时 Watcher 会**自动备份并重建**它（备份为 `profile.corrupt-<时间>`，每天最多一次），一般不需要手动删除。
- `health.json` — Watcher 的健康状态（上次成功取数时间、连续失败次数、最后错误、profile 体积、剩余磁盘），供 `docker healthcheck` 与配置页显示。
- `stores.json` — 轮询时发现到的门店名，供界面选项。

### 4. 在界面里配置

打开 `http://<宿主机>:8787`：

- **轮询设置**：间隔秒数（下限 30，每轮再叠加 0~10 秒随机抖动）。
- **告警推送地址**：Watcher **自身**出问题（连续 3 轮取不到数据）时推到这里。与各推送目标分开，避免运维消息混进业务推送。**强烈建议填写** —— 不填的话 Watcher 失联时你不会收到任何通知。配置页会在未填写时给出提示。
- **推送目标**：每个目标独立关注一套 SKU 与门店，可推到不同 Bark 地址。
  - SKU 与门店均必填。
  - 新增 / 重新启用目标会**立即推一次全量**（用上一轮缓存快照，约 2 秒）；之后只在库存变动（补货 / 售完）时推送。
  - 禁用后再启用会再推一次全量。
  - 门店选项只显示 6 家官方中文店名；推送文案用中文，内部用英文 slug 对齐 Apple 返回数据。

### 5. 环境变量（一般无需改）

- watcher：`LOCATION`（默认 `中環`）。
- web：`CONFIG_PATH`（默认 `/data/config.json`）、`SKUS_PATH`（默认 `/app/skus_hk.json`）、`HOST` / `PORT`（默认 `0.0.0.0:8787`）。

### 6. 健康检查

Watcher 不再只是「进程还活着就算好」——它会写 `data/health.json`，配置页顶部也直接显示状态。

- 容器健康与否：`docker inspect --format '{{.State.Health.Status}}' astorewatcher-watcher-1`。
  判据是「距上次成功取数不超过 15 分钟」（`main.py health --max-age 900`）。
- 手动查看：`docker compose exec watcher python3 main.py health`。
- 配置页顶部显示：上次成功检查时间、距今多久、本轮耗时、profile 体积、剩余磁盘、
  连续失败次数与最后错误；未填告警地址时也会提示。每 60 秒自动刷新。
- 连续失败达 3 次会通过「告警推送地址」发 Bark，恢复后发一条「已恢复正常」；
  同一轮故障 30 分钟内只报一次，不会反复轰炸。
- watcher 日志：`docker logs astorewatcher-watcher-1` 应出现 `本轮完成：N 个 SKU`。

### 7. 排障

排障时请**在容器内**用与生产相同的路径，否则看到的行为可能不一样：

```bash
docker compose exec watcher python3 main.py once   # 走同一条 supervisor/worker 链路
```

浏览器是被隔离在 `worker.py` 子进程里的：父进程每轮 spawn 它，硬超时 225 秒，
超时就掐断整个进程组。所以「浏览器挂死让 Watcher 卡住不动」这种情况不会再发生 ——
最多丢一轮数据，下一轮自动重来。

