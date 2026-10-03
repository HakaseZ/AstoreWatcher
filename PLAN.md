# 容器化方案规划（Docker + 浏览器）

> **本文件是规划与决策的历史记录**，记录项目怎么分阶段做起来、当时确定了什么、有哪些风险。
> 想看**当前代码实际是什么样**（架构、模块职责、机制原理），请去 `docs/architecture.md`；
> 想看**怎么操作**（构建、启动、配置、排障），请去 `README.md`。

## 0. 阶段进度（2026-10-03 更新）

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | spike：容器里的浏览器能不能拿到数据 | ✅ 已完成（2026-09-29），结论见 4.1 |
| M1 | 最小可用镜像 | ✅ 已完成（2026-09-29） |
| M2 | 常驻轮询 + Bark 通知 + 配置界面 | ✅ 已完成 |
| M3 | 稳健性（子进程隔离 + 健康检查与告警） | 🔄 进行中 —— 即分支 `feat/watchdog-hardening` 的 PR #3 |

> M2 完成后参数与下面 5 节的原始描述已有出入（间隔下限、抖动、监听地址、CLI 子命令等），
> **以代码和 `docs/architecture.md` 为准**；差异处已在下文就地标注。

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

### 2.1 M3（稳健性）追加的决策

| 项 | 决策 |
|---|---|
| 浏览器故障隔离 | 采用**子进程隔离**（`supervisor` + `worker`），不用 watchdog 线程 —— 线程内的 `ctx.close()` 走 CDP 通道，浏览器卡死时送不进去 |
| 告警通道 | 走**独立的 `health_webhook`**，不复用各推送目标的 `bark_url`，避免运维消息混进业务推送流 |
| 告警未配置时 | **仅配置页显示警示**，不做任何降级（不打日志、不复用目标地址） |
| 故障恢复后 | 推一条「**已恢复正常**」通知 |
| SIGTERM 到达 | **立即中断当前轮**，不等它跑完（否则 60s 宽限期不够，容器会被 SIGKILL 而留下陈旧锁） |
| 单轮硬超时 | **225s** |
| 健康状态展示 | **做进 web 配置页**（含「未配置告警」警示），而非只有日志 |
| 旧生产 profile | 不迁移 —— 用户整体重建生产环境，本方案按新 profile 冷启动设计 |

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

**✅ 已完成（2026-09-29）**

```
Dockerfile              # FROM python:3.13-slim + apt chromium + pip playwright → 978MB
docker-compose.yml      # shm_size 2gb、./data 挂 profile、LOCATION/TZ 环境变量
requirements.txt        # playwright==1.63.0
main.py                 # 加载 skus_hk.json → 3 批查询 → 打印结果后退出
```

**实测结果**：`docker compose run --rm watcher` → 32 个 SKU × 6 家门店全部取到，
其中 7 个 SKU 显示可取貨（如 iPhone 18 Pro 2TB 黑色六店全有货）。

**构建/运行的两个坑（已处理）**
1. **基底怎么来的**：本机 docker.io 直连与公共加速站全不可用；
   经 `public.ecr.aws/docker/library/python:3.13-slim` 拉取后本地打 tag，
   Dockerfile 里仍写 `FROM python:3.13-slim`，可移植性不受影响。
2. **冷启动 541 会污染 profile**：新 profile 首次被判机器人后，该 profile 会**持续** 541。
   main.py 的处理：先 reheat（重开购买页重试）→ 仍全批失败则 `rmtree` 掉 profile 重建再跑一轮（实测有效）。

镜像体积 978MB（spike 镜像 2.76GB），主要来自 chromium 本体。
`restart: "no"`（M1 单次运行），M2 改常驻轮询时切 `unless-stopped`。

### M2 — 常驻轮询 + Bark 通知 + 配置界面

> 状态：**✅ 已完成**（实施顺序 M2a → M2b → M2c 均已落地）
>
> 落地时与本节原始方案的差异（**以代码为准**）：
> - 轮询间隔下限是 **30s**（不是 60s），抖动 **0~10s**（不是 0~30s），配置里**没有** `jitter_sec` 字段；
> - web 实际绑 **`0.0.0.0:8787`**（不是 `127.0.0.1`），这是 `SECURITY.md` 里的已知漏洞 1；
> - `main.py` 现有四个子命令：`poll` / `once` / `web` / `health`（原方案只有 poll / web）；
> - 多目标的 CRUD 仍存在，但配置以**整份 `config.json` 回传**为主；另有 `/api/health`、`/api/stores`。
>   当前架构与设计说明见 `docs/architecture.md`。

#### 需求

常驻轮询并在库存状态变化时推 Bark；另加一个前端界面配置推送：录入 Bark 地址、
为该地址勾选它要关注的 SKU。**多目标模型**：可有多个 Bark 地址，各自独立配一套 SKU。

> 本节原本还列了当时的架构图、文件职责表、配置模型、API 清单、界面结构与推送判定。
> 这些内容**现已全部落地，其当前形态以 `docs/architecture.md` 为准** ——
> 本文件是历史方案、那边是当前实现，两边重复维护必然漂移，故此处不再保留细节。

#### 实施顺序（已完成）

- **M2a 后端骨架**：抽 `browser.py`、`config.py`、`state.py`、`notifier.py`、`poller.py` + CLI，
  先用命令行跑通多目标推送
- **M2b 界面**：`web.py` API + `static/index.html`
- **M2c 收尾**：compose 双 service、`.gitignore`、`config.example.json`、README 用法、PLAN 更新

#### 阶段风险（当时的预估）

| 风险 | 应对 / 现状 |
|---|---|
| 界面与轮询同时改 config.json | 原子写；watcher 解析失败时沿用旧配置并记日志（已落地） |
| 常驻后请求量增大触发风控 | 间隔下限 30s（原案 60s）+ 抖动；每轮固定 3 批（已确认覆盖全港 6 店）；**M3 的指数退避尚未实现** |
| Bark key 泄露 | config.json 不入库；web 实际绑 `0.0.0.0`，属 `SECURITY.md` 已知漏洞 1 |

### M3 — 稳健性

cookie 失效自动重开购买页、**指数退避重试**、SIGTERM 优雅退出、结构化日志、健康检查。

> 状态：**🔄 进行中**（分支 `feat/watchdog-hardening`，PR #3）。已落地：子进程隔离、
> SIGTERM 优雅退出、健康检查与告警、失败重试阶梯、profile 重建护栏、僵尸进程回收。
> **未做**：指数退避（当前是固定最多 3 次尝试、重试间无退避）。
> 机制说明见 `docs/architecture.md` 第 5 节，不再在此展开。

## 6. 尚待确认

### ✅ 已确认：香港门店范围（2026-09-29 实测）

香港一共 **6 家** Apple Store，`location` 参数**只影响排序（按距离），不影响返回的门店集合**：

| 门店 | 地区 |
|---|---|
| ifc mall | 中環 |
| Canton Road | 尖沙咀 |
| Causeway Bay | 銅鑼灣 |
| Festival Walk | 九龍塘 |
| apm Hong Kong | 觀塘 |
| New Town Plaza | 沙田 |

实测方式：用中環 / 沙田 / 銅鑼灣 / 尖沙咀 / 觀塘 五个 location 查同一个 SKU，
返回集合完全一致、仅顺序不同。

**影响**：一次查询即可覆盖全港，无需按地点查多遍 —— M2 轮询每轮固定 3 批请求即可，
省掉 2/3 的请求量，也降低触发风控的概率。`location` 取值可写死为 `中環`。

### 仍未确认（不阻塞 M2）

- 是否监控全部 32 个 SKU（界面已支持按目标自由勾选，此项转为使用偏好）
- Bark key 放环境变量还是 `config.json`（已定：config.json，但文件不入库）

## 7. 风险登记

| 风险 | 影响 | 应对 |
|---|---|---|
| headless / 指纹被识别 | 方案根基失效 | M0 实测解决：CDP 覆盖 UA + Client Hints；退路 headful+Xvfb 保留 |
| 冷启动 541 污染 profile | 该 profile 持续 541 | M1 已实现：全批失败时重建 profile 重试 |
| 轮询触发限流 | IP 被封、数据失真 | M3 退避（**尚未实现**，当前重试无退避）；间隔待压测 |
| 接口只有状态没有台数 | 无法做"剩余 N 台"告警 | 已确认 Apple 不公开；如需台数需另找数据源 |
| 基础镜像 tag 漂移 | 构建不可复现 | 钉死版本号 |

### 7.1 M3 追加的风险与回滚

| 风险 | 影响 | 应对 / 现状 |
|---|---|---|
| `start_new_session=True` 的进程组归属 | 若 `os.getpgid(pid) != pid`，`killpg` 会打到父进程自己的组，当场把服务打死 | 已确认 worker 自成组长；另加一层保险：worker 绝不写任何持久状态文件 |
| rebuild_profile 误触发 → 永久 541 | 每轮都重建、不停丢 cookie 的死循环 | LevelDB 特征串是**未经本机实测的推断**（本地不启真实浏览器）；先用备份的 profile 副本打出真实 stderr 再确认。24h 预算钉成硬闸 |
| `health_webhook` 被静默清空 | 用户一保存配置就抹掉告警地址，而那恰好是最该告警的时刻 | 前端必须同步这个字段；已列入 `README.md` 验证清单第 6 步 |
| **僵尸进程撑满 PID cgroup** | 连 `fork` 都失败 → 反复 `START_FAILED`，监控静默失效 | **已真实发生**（累积 9349 个僵尸）：容器 PID 1 从不 `wait()` 被 reparent 的 chromium。已修：`reap_orphans()` + worker 设为 subreaper。详见 `docs/architecture.md` 5.5 |
| 浏览器挂死 | 主循环永久阻塞 | 硬超时 225s + 整组 `killpg`（M3 已落地） |

**回滚**：切回 `origin/main`。
