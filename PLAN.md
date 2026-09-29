# 容器化方案规划（Docker + 浏览器）

> 状态：**M0 spike 已完成（2026-09-29），容器方案验证通过**；M1 起待实施

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
| 基础镜像 | ~~Playwright 官方镜像~~ → **自建**：slim 基底 + apt chromium + pip playwright（MCR 官方镜像 762MB 大层实测仅 ~100KB/s，且官方 CDN 不可用，见 M0） |
| headless 失败退路 | **已实测（M0）：headless + CDP Client Hints 覆盖即可**，Xvfb/headful 退路用不上 |
| 通知渠道 | **Bark**（iOS 推送，`POST https://api.day.app/push`） |
| 生产镜像形态 | **最小体积，纯 Dockerfile + docker-compose 构建部署**（用户已定，M1 落实） |
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

### 4.1 headless 能否过防护 —— **已由 M0 实测解决（2026-09-29）**

原风险是 headless 指纹被识别。实测结论：
裸 headless（无论是否 headful、是否改 UA 字符串）都会被拦（541 且不下发任何防护 cookie）；
**用 CDP `Network.setUserAgentOverride` 整套覆盖 UA + Client Hints（`userAgentMetadata`）后，
headless 即可 200**。识别根源是 Chromium 构建的 `Sec-CH-UA` 自报 "Chromium" 与 UA 冒充 Chrome 自相矛盾。
详见第 5 节 M0 实测结论。原三档递进方案作废，实际只需一档。

### 4.2 cookie 续命

实测防护 cookie 为 Apple shield 的 `shld_bt_m` / `shld_bt_ck` / `sh_spksy`（非 Akamai 的 `_abck`/`ak_bmsc`）。
容器内解法：

- user-data-dir 挂 volume（`./data/profile`），cookie 跨重启保留
- 检测到 541 或返回空 → 自动重跑一次 `goto` 购买页重热（M0 实测 reheat 有效）
- 连续失败 → 重建 profile（541 具有状态性，坏 profile 会持续 541）

### 4.3 网络出口

- 容器需 outbound 到 `apple.com`
- **未验证**：非香港出口 IP 能否查香港门店库存
  （MD 中 `location=中環` 返回 6 家门店，推测不强制，但 Akamai 对不同地区 IP 策略可能不同）
- M0 会顺带确认

## 5. 分阶段实施

### M0 — spike：只回答「容器里的浏览器能不能拿到数据」

**✅ 已完成，结论：通过（2026-09-29）**

#### 实测结论

| 项 | 结果 |
|---|---|
| 容器内 curl 裸请求 | HTTP 541（网络层通，被防护拦，符合预期） |
| 裸 headless / headful+Xvfb（仅改 UA 字符串） | 全部 541，且防护体系**一个 cookie 都不下发** |
| headless + **CDP Client Hints 一致性覆盖** | **HTTP 200，直接拿到全部数据，无需 Xvfb/headful** |

#### 关键发现（修正了此前的认知）

1. **拦截体系已不是 Akamai JS 挑战**：实测页面无 Akamai sensor 脚本，`_abck`/`ak_bmsc`/`bm_sz` 全程不下发。
   现役体系是 Apple 自家 shield（`/shop/shld/v2_1/verify.js` → `shld_bt_m`/`shld_bt_ck`/`sh_spksy` cookie）。
   `apple_hk_18_models.md` 里的 Akamai 记录已过时。
2. **决定性识别点是 Client Hints 不一致**：UA 冒充 Chrome 但 `Sec-CH-UA` 自报 `"Chromium"`（Chromium 构建的行为），
   被边缘直接判机器人。解法：CDP `Network.setUserAgentOverride` 把 UA + `userAgentMetadata`
   （brands 含 "Google Chrome"、fullVersionList、platform 等）整套覆盖成一致的 Google Chrome。
   Playwright 的 launch 参数 `user_agent` **做不到**这一点，必须走 CDP。
3. **headless 可用**，无需 headful+Xvfb 兜底（该退路保留但用不上）。
4. **网络出口无影响**：默认 bridge 网络与 `--network host` 行为一致，非港 IP 也能查（`location=中環` 返回 6 家门店，与 MD 记录一致）。
5. **541 具有状态性**：早期失败会话后，同一 profile 持续 541；换全新 profile + 稳定通过若干次后恢复。
   轮询实现应：541 → 重新 goto 购买页（reheat 有效，实测第二次访问即 200）→ 仍失败则考虑重建 profile。
6. **真实返回结构**（修正 MD）：`body.content.pickupMessage.stores[].partsAvailability[<PART>]`
   → `pickupDisplay`（available/unavailable）+ `pickupSearchQuote`（今天可取貨/暫無供應）。
   顶层没有 `partsAvailability`；`%2F` 转义与裸斜杠均可用。

#### 实测判据（PASS 输出）

- 镜像 `astore-m0:v1`（spike 临时镜像，见下）
- `spike/m0.py --tier 2 --executable /usr/bin/chromium --chrome-brand`
- HTTP 200 + 6 家门店（ifc mall / Canton Road / Causeway Bay / Festival Walk / apm / New Town Plaza）
- `MJXV4ZA/A` 全部 `unavailable`（暫無供應）——当天实际无货，字段结构验证正确

#### 环境与工程备注

- spike 测试镜像 `spike/Dockerfile.m0`：基于本地已有 `devcontainers/python:3-3.14-trixie` +
  apt 装 Debian chromium 150 + xvfb + pip 装 playwright 1.63.0（跳过官方浏览器下载）。
  **不是生产镜像**——2.76GB。
- 网络约束（本机实测）：MCR 官方镜像大层仅 ~100KB/s（已放弃）；清华 apt 源 44MB/s、PyPI ~863KB/s；
  Playwright 官方 CDN 与 npmmirror binaries 不可用 → 生产镜像也走 apt chromium + pip playwright 路线。
- **生产镜像要求（用户已定）**：最小体积，纯 Dockerfile + docker-compose 构建部署（M1 落实，可用 `python:3.13-slim` + apt chromium 为基底）。
- spike 产物：`spike/m0.py`（参数化探测脚本）、`spike/bisect.py`（最小可复现配方）、`spike/Dockerfile.m0`；`data/` 已加入 `.gitignore`。

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
