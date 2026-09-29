# 容器化方案规划（Docker + 浏览器）

> 状态：**待评审，尚未执行任何实现**
> 本文件只做规划。M0 spike 开始前需再次取得许可。

## 1. 目标与背景

项目要定时轮询 Apple 香港门店的 iPhone 18 取货状态。
`apple_hk_18_models.md` 已实测出 5 条硬约束，其中决定性的一条是：

> 库存接口受 Akamai Bot Manager 保护，curl / curl_cffi 全部被 HTTP 541 拦截，
> **必须由真实浏览器执行 JS 挑战**才能拿到 `_abck` / `ak_bmsc` cookie。

因此本项目的容器化**不是"打包一个脚本"**，而是：
**在容器里稳定地跑一个能过 Akamai 的 Chrome。**

## 2. 已确认的决策

| 项 | 决策 |
|---|---|
| 部署目标 | 先按**本机**做（amd64），可移植性由 Docker 保证 |
| 基础镜像 | **Playwright 官方镜像** `mcr.microsoft.com/playwright/python`，Chromium 与系统依赖预装 |
| headless 失败退路 | **先做 M0 spike 实测**，拿到结果再决定（不预先假设） |
| 通知渠道 | **Bark**（iOS 推送，`POST https://api.day.app/push`） |
| 分支策略 | 严格遵守 `AGENTS.md`：独立分支 → 我决定 push / PR → 我不自行合并 |

## 3. 架构骨架

```
[单个容器]
  Playwright(Chromium)  ←→ 持久化 profile (volume ./data/profile)
        │
   1. goto  https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro
            └─ 真实浏览器执行 Akamai JS 挑战 → 下发 _abck / ak_bmsc
   2. page.evaluate(fetch) 在页面上下文内请求
      /hk-zh/shop/fulfillment-messages
        ?fae=true&pl=true&mts.0=regular&mts.1=compact
        &parts.0..14&location=中環
   3. 32 个 SKU 拆 3 批（15 / 15 / 2，单批上限 15）
   4. 与上次状态比对 → 变化才推 Bark
```

该流程天然满足 MD 中的 5 条硬约束：
域名用 `apple.com/hk-zh`、真实浏览器、在 HK 页面上下文内 fetch、
cookie 自动获取、单批 ≤15。

## 4. 关键技术点

### 4.1 headless 能否过 Akamai —— 最大风险，未知

Playwright 默认 headless 带 `navigator.webdriver` 等自动化指纹，Akamai 可能照样 541。
缓解手段按强度递增，逐档实测：

1. 新版 headless（`--headless=new`，UA 与真实 Chrome 一致）
2. `launch_persistent_context` + `--disable-blink-features=AutomationControlled`
   + `locale=zh-HK` + `timezone=Asia/Hong_Kong` + 真实 UA / viewport
3. 兜底：**headful + Xvfb**（容器内虚拟显示跑真窗口 Chrome），过 Akamai 概率最高，镜像多装 xvfb

**结论必须由实测给出，不靠推断。**

### 4.2 cookie 续命

MD 记录 cookie 会过期、过期后需重新打开购买页。容器内解法：

- user-data-dir 挂 volume（`./data/profile`），cookie 跨重启保留
- 检测到 541 或返回空 → 自动重跑一次 `goto` 购买页重热

### 4.3 网络出口

- 容器需 outbound 到 `apple.com`
- **未验证**：非香港出口 IP 能否查香港门店库存
  （MD 中 `location=中環` 返回 6 家门店，推测不强制，但 Akamai 对不同地区 IP 策略可能不同）
- M0 会顺带确认

## 5. 分阶段实施

### M0 — spike：只回答「容器里的浏览器能不能拿到数据」

| 步骤 | 动作 | 判据 |
|---|---|---|
| 1 | 容器内 `curl` 打 `fulfillment-messages` | 拿到 **541 即为好消息**：网络层通、只是被 Akamai 拦 |
| 2 | 起 Playwright persistent context，设 zh-HK / HK 时区 / 真实 UA / 反自动化 flag | — |
| 3 | `goto` 购买页拿 cookie，再 `evaluate(fetch)` 查单个 SKU `MJXV4ZA/A`，`location=中環` | fetch 成功 + `partsAvailability` 非空 + `pickupDisplay` 有值 |
| 4 | 失败则换 headful + Xvfb 重测 | — |

产出：一份实测结论（通过 / 541 / 空数据 + 截图或响应片段）。
**M0 是整个方案的唯一取舍点**：若容器浏览器过不了 Akamai，
方案降级为"容器只跑轮询逻辑、CDP 连宿主机 Chrome"，或重新评估。

### M1 — 最小可用镜像

```
Dockerfile              # FROM mcr.microsoft.com/playwright/python:<钉死 tag>，不用 latest
docker-compose.yml      # restart: unless-stopped，./data 挂为 volume
requirements.txt
main.py                 # 加载 skus_hk.json → 3 批查询 → 打印结果后退出
```

### M2 — 轮询 + 通知

```
main.py        # 入口、CLI、装配
browser.py     # Playwright 会话管理、cookie 重热
poller.py      # 分批查询、轮询间隔 + 随机抖动
state.py       # 上次状态持久化（防重复告警）
notifier.py    # Bark 推送
config.json / config.example.json
```
`.gitignore` 追加：`data/`、`config.json`（含 Bark key）

### M3 — 稳健性

cookie 失效自动重开购买页、指数退避重试、SIGTERM 优雅退出、结构化日志、健康检查。

## 6. 尚待确认（不阻塞 M0）

- 监控哪些 `location`（目前文档示例为 `中環`，返回 6 家门店）
- 是否监控全部 32 个 SKU，还是只盯部分机型/颜色/容量
- 轮询间隔（文档建议 ≥60s + 抖动，未压测过限流）
- Bark key 放环境变量还是 `config.json`

## 7. 风险登记

| 风险 | 影响 | 应对 |
|---|---|---|
| headless 被 Akamai 识别 | 方案根基失效 | M0 实测；退路 headful+Xvfb 或连宿主 Chrome |
| 轮询触发限流 | IP 被封、数据失真 | M3 退避；间隔待压测 |
| 接口只有状态没有台数 | 无法做"剩余 N 台"告警 | 已确认 Apple 不公开；如需台数需另找数据源 |
| 基础镜像 tag 漂移 | 构建不可复现 | 钉死版本号 |
