# AstoreWatcher 架构说明

> 本文描述**当前代码实际是什么样**：系统怎么分层、每层的职责与关键机制、以及这些机制为什么这样设计。
>
> 其它文档各司其职，不重复本文内容：
>
> | 文档 | 负责什么 |
> |---|---|
> | `README.md` | **操作**：怎么构建、启动、配置、看健康状态、排障 |
> | `PLAN.md` | **历史**：项目怎么分阶段做起来的、当时的决策与风险登记 |
> | `apple_hk_18_models.md` | **数据**：型号/SKU 对照表，以及库存接口的实测硬约束 |
> | `SECURITY.md` | **安全**：已知漏洞与对外开放前的检查清单 |
> | `AGENTS.md` | **规则**：给 AI 助手的硬性工作约定 |

---

## 1. 系统概览

定时轮询 Apple 香港门店的 iPhone 取货状态，状态变化时推 Bark 通知。

一个镜像、两个服务，共享宿主机 `./data`（挂载为容器内 `/data`）：

```
watcher  python3 main.py poll   常驻轮询；唯一会开浏览器的服务
web      python3 main.py web    配置界面（FastAPI + 原生 JS 单页）；不碰浏览器
```

**为什么拆两个服务**：浏览器崩溃 / 挂死 / 泄漏只影响 watcher；改配置、重启界面不会打断轮询。
两者共享 `/data`，所以界面改完配置下一轮就生效（watcher 按 mtime 热加载，无需重启）。

数据目录里各文件的用途见 `README.md`（运维视角）；本文只关心**谁读写它们**。

---

## 2. 一轮数据的完整流转

```
定时器（interval_sec + 0~10s 抖动）
  └─ poller.run_once                     业务决策层
       ├─ 读 config.json / init.json / state.json
       └─ supervisor.run_round           进程管理层（不 import playwright）
            └─ spawn python3 worker.py --spec <json> --out <json>
                 └─ browser.BrowserSession   唯一开 chromium 的地方
                      ├─ 打开购买页过 Akamai/shield → 拿 cookie
                      └─ page.evaluate(fetch) 分批查库存接口（单批 ≤15）
                 └─ 写 out JSON → exit(code)
            └─ 读 out JSON → RoundResult（超时则掐断整组重试，最多 3 次）
       ├─ state.diff 比对上次快照 → 得到「变化」
       └─ notifier 编排文案 → POST Bark
  └─ 写 health.json / state.json / init.json
```

**分层的硬约束**（下面各节会解释为什么）：

- `poller` 不直接接触 playwright——浏览器的一切都关在 worker 里。
- `supervisor` 也不 import playwright——这样它能在没有浏览器的环境里被测试。
- `worker` **绝不写** `state.json` / `init.json` / `health.json`——被 SIGKILL 时它不能留下半截状态。

---

## 3. 模块职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `main.py` | CLI 入口：`poll`（常驻）/ `once`（单次查询打印）/ `web`（界面）/ `health`（健康检查） | `once` 与生产走**同一条** supervisor+worker 路径，否则排障看到的行为会和生产不一致 |
| `poller.py` | 每轮的业务决策：算标记增删 → 取数 → 比对 → 逐目标推送；SIGTERM 到达时立刻中断当前轮 | 单轮异常不能让常驻进程死掉，但必须被记进 health 并告警 |
| `supervisor.py` | worker 的生死：spawn、硬超时掐断、按退出码重试/收工/重建 profile、写 `health.json`、发 Bark 告警 | 不 import playwright |
| `worker.py` | 子进程入口：环境预检 → 单例锁判定 → 起浏览器取数 → 原子写 out JSON → `exit(code)` | 唯一开浏览器的进程；不写任何持久状态 |
| `browser.py` | 取数配方（persistent context + CDP 覆盖 + reheat + 页面失效重建）与 profile 备份 | 见 `apple_hk_18_models.md` 的实测硬约束 |
| `config.py` | 读写 `config.json`（原子写 + mtime 热加载）、白名单校验与 clamp；统一 `HK_TZ` / `hk_now()` | 时区常量放这里，worker 只依赖 config、不碰 FastAPI |
| `state.py` | `state.json` 存上次快照；`diff` 出变化、`initial_snapshot` 出新增 SKU 的当前状态；`init.json` 记首推标记 | 写盘加锁，避免与 web 进程并发写互相覆盖 |
| `notifier.py` | 文案编排（merged / per_store）+ Bark POST（标准库 urllib，不引 requests） | 纯文案函数保持纯净可断言；检查时刻只在实际发送那一刻拼上 |
| `web.py` + `static/index.html` | 配置 API 与单页界面，无构建链 | 只读 `health.json` 做展示，不判断健康（那是 `main.py health` 的事） |

---

## 4. 取数链路

三件事缺一不可，少一件就会被判机器人返回 541（实测结论，详见 `apple_hk_18_models.md`）：

1. **persistent context + 挂载 profile** —— 让 shield cookie 跨重启保留；
2. **CDP 覆盖 UA + Client Hints** —— Playwright 的 `user_agent` 参数改不了 `Sec-CH-UA`，
   而 Chromium 构建会自报 `"Chromium"`，与冒充 Chrome 的 UA 自相矛盾，必被拦；
3. **541 → 重开一次购买页 reheat** —— 实测有效。

另外两个已知坑：

- **冷启动污染**：新 profile 若首次就被判机器人，该 profile 会**持续** 541，只能丢弃重建（见第 6 节重建阶梯）。
- **页面会自己失效**：浏览器可能因崩溃恢复或内存回收关掉页面、或导航走（风控跳转），
  所以每批取数前都检查 `page.is_closed()`，失效则重建页面并**重新做一次 CDP 覆盖**（`new_page` 不继承上一次的覆盖）。

单批上限 15（Apple 静默丢弃超出的 part），32 个 SKU 拆 3 批。

---

## 5. Watcher 健壮性：把浏览器隔离到子进程

### 5.1 为什么要子进程

`page.evaluate` 在 Playwright sync API 下**不受任何 timeout 约束**——浏览器一旦挂死，
调用会永久阻塞，主循环再也不推进一步，连 SIGTERM 都来不及处理。只有父进程从**外部** `killpg` 才能真正打断它。

于是「浏览器可能出的所有毛病」（崩溃、OOM、内存/句柄泄漏、profile 损坏……）最终都表现为同一件事：
**worker 进程退出**。父进程只需一个统一的「重启重跑」动作就能兜底，连还没想到的失败模式一起兜住。

### 5.2 进程模型与 IPC

- `start_new_session=True`：worker 自成进程组组长（`pgid == pid`），之后才能整组 `killpg` 连带 chromium，
  又不会误伤父进程自己所在的组。
- **IPC 用 JSON 临时文件而非 stdout**：挂死时 worker 根本不产生 stdout，父进程无法区分「挂了」和「输出为空」；
  stdout 还有缓冲、部分写、半截 JSON 的问题。落盘 + 原子改名天然可判定：**out 文件不存在 = worker 没跑完**。
- worker 的 stdout/stderr 直接继承父进程，日志进 docker log。

### 5.3 worker 退出码契约

| code | 常量 | 含义 | 父进程动作 |
|---|---|---|---|
| 0 | `EXIT_OK` | 取到数据、无失败批次 | 正常推送 |
| 3 | `EXIT_PARTIAL` | 有失败批次但 snapshot 非空 | 正常推送 + warn |
| 9 | `EXIT_INTERNAL` | worker 自身未捕获异常 | 可重试 |
| 10 | `EXIT_LOCK_STALE` | 陈旧 SingletonLock（worker 已清理） | 可重试 |
| 11 | `EXIT_LOCK_LIVE` | 有**活着的** chromium 持有该 profile | **不重试、不擅自杀**（可能是人在手动排障） |
| 12 | `EXIT_PROFILE_CORRUPT` | profile 损坏（LevelDB 特征） | 走重建预算；无预算则收工告警 |
| 13 | `EXIT_START_FAILED` | 启动失败但原因不明 | 可重试 |
| 14 | `EXIT_PRECHECK` | 环境预检失败（可执行文件/权限/磁盘） | 立即收工告警，不重试 |
| 15 | `EXIT_FETCH_CRASHED` | 取数途中浏览器失联 | 可重试 |
| 16 | `EXIT_TERMINATED` | 被父进程主动中断（正常信号） | 不计入失败 |

- 可重试码：`9 / 10 / 13 / 15`；致命码：`11 / 14`；可重建码：`12`。
- 重试上限 `MAX_ATTEMPTS = 3`（换一个新 worker 进程再试，最多 3 次尝试）。
- 除 0/3 外，worker 必须写出 out 文件（带 `kind` / `error`），否则父进程无法区分「崩了」和「没写成功」。
- 单轮硬超时 `ROUND_TIMEOUT = 225s`：推导 = 启动+首开页 75s + 每批最坏 45s × 3 批 + 收尾 15s。正常轮次 3~8s。

out 文件字段：`{ok, checked_at, snapshot, failed, kind, error}`。
`checked_at` **必须由 worker 在取数完成时计算**（不是父进程）——同一份 snapshot 会分发给多个目标，
各次推送相隔十几秒，但它们的检查时刻必须是同一个。

### 5.4 失败模式对策（落点）

- **启动前**：扫 `/proc/*/cmdline` 找持有该 profile 的活进程（比读 `SingletonLock` 可靠，锁可能早已陈旧）；
  陈旧锁则清 `Singleton*` 三件套；预检可执行文件、目录可写、磁盘余量（<200MB 告警）。
  `chromium_version()` 取不到版本**一律抛错**，不静默回退假版本号——那会让 UA/Client Hints 自报版本与真实不符，
  更容易被判机器人，也会掩盖「可执行文件坏了」。
- **运行期**：崩溃 → 退出码 15 → 重开；挂死 → 硬超时掐断整个进程组。
- **退出期**：`stop_grace_period: 60s` 配合「父进程收到 SIGTERM 立刻中断 worker 进程组」——
  若等这一轮自然跑完（最坏 225s），容器必然被 SIGKILL，chromium 来不及释放锁。代价只是丢一轮数据。

### 5.5 进程组清理与僵尸回收

这是**真实踩过的生产事故**，机制比看上去讲究：

**清理顺序必须 SIGTERM → 等 → SIGKILL**：SIGTERM 让 chromium 走正常 teardown，
干净释放 SingletonLock 并 flush profile 的 LevelDB；直接 SIGKILL 会让下一次启动踩到陈旧锁或 LevelDB 损坏。

但「等」的判据和「SIGKILL 给谁」有两个坑，都已修掉：

1. **SIGKILL 必须无条件发给整个进程组，不能看 worker 自己有没有退出。**
   worker 完全可能响应 SIGTERM 后正常退出，而它的 chromium 子进程还活着。
   若因为「worker 已经死了」就跳过整组 SIGKILL，残留的 chromium 会继续持有 profile，
   下一轮 worker 检测到活进程 → 报 `LOCK_LIVE` 且**不重试** → 监控卡死。
   所以 pgid 要在发 SIGTERM **之前**就存下来（worker 退出后 `proc.pid` 就没了，届时再取 pgid 会失败）。
2. **等待要撑到 grace 用完，或「整组已空」为止，不能 worker 一退出就停。**
   worker 可能先走而 chromium 还在 teardown，提前 SIGKILL 就等于打断它的收尾；
   反过来整组已经空了也没必要干等（`_group_alive()` 用 signal 0 做存在性检查）。

**僵尸回收（事故本身）**：容器里 `main.py poll` 是 **PID 1**。worker 自成进程组，chromium 是它的孙进程；
worker 一退出，chromium 被内核 reparent 到 PID 1，而 PID 1 从不 `wait()` ——
死掉的 chromium 就永远挂成僵尸，把 PID cgroup 一点点撑满，最终连 `fork` 都失败
（`Resource temporarily unavailable` / `Cannot fork`），表现为反复 `START_FAILED` 的死亡螺旋。
（本次事故里累积到 9349 个僵尸。）

两道防线：

- `supervisor.reap_orphans()`：每轮 worker 被 `proc.wait` 回收后，`waitpid(-1, WNOHANG)` 扫一遍死掉的子进程。
  **只在 worker 已被回收之后调用**，所以不会误收 worker 本身。
- worker 启动时 `prctl(PR_SET_CHILD_SUBREAPER)`：chromium 退出后改 reparent 到 worker 自己，
  由 `BrowserSession.close()` 里的 `waitpid` 即时回收。

### 5.6 profile 重建阶梯（护栏）

profile 里的 shield cookie 很珍贵，重建有被判机器人的风险，所以是最后手段：

1. 连续 **3 轮**「浏览器正常跑完却一个数据都没拿到」且今日还有预算，才重建（避免一次接口抖动就丢 cookie）。
2. 预算：`data/profile_rebuild.json` 记 `{date, count}`，**每 24h 上限 1 次，同一轮内绝不超过 1 次**。
3. 重建不是删除，而是**重命名备份**为 `profile.corrupt-<时间戳>`，留人工捞回 cookie 的机会；
   只保留最近 **3 份**备份（旧的从不清理的话，按每天一次、单个约 30MB 算，一年能堆到十几 GB）。

另外 `docker-compose.yml` 把 hostname 钉死为 `astore-watcher`：Chromium 判定 SingletonLock 时是
**先比 hostname，不符就直接 exit 21**，连那个 pid 是否活着都不检查。容器 hostname 默认是短 ID、
每次重建都变，于是残留的旧锁永远无法自行失效。钉死后 hostname 相符，Chromium 会继续验 pid，
pid 已死就自动接管 —— 陈旧锁从此能自愈。

---

## 6. 推送

- **两种编排**（`push_mode`）：
  - `merged`（默认）：按型号分组，同一 SKU 的多家门店合并进一行（三家店补货原本 9 行 → 2 行）；
    每条最多列 5 个 SKU，超出补「等 N 條」。
  - `per_store`：按「**门店 + 方向**」分条，一家店最多两条（有貨了 / 無貨了，有货在前）。
    同一家店同时有补货和售罄时**必须拆成两条** —— 混在一条「有貨了」里、正文却夹着「暫無供應」，
    读的人根本不知道到底能不能去买。只有有货那条带下单链接。
- **每家门店要带上它自己的 quote**（如「備妥於： 今日」，来自 Apple 的 `pickupSearchQuote`）：
  否则只剩「可取貨」和店名，看不出什么时候能取。
- **检查时刻** `checked_at` 只在**最终发送层**拼到正文末尾，不塞进纯文案函数（保持那些函数可严格断言）。
- **门店名**：界面用中文店名，推送文案也用中文；内部一律归一到英文 slug 再与 Apple 返回的数据对齐。
- **首推 vs 变动**：目标首次加入（或重新启用）推一次全量（含无货），标记写进 `init.json`，
  推送失败则不落标记、下轮重试；此后只在状态变化时推（`notify_on` 控制要不要推「已售完」）。
- **空快照护栏**：本轮没取到任何关注中的 SKU 时**不推送也不落标记**，避免发出「共 0 个 SKU」的废纸推送
  并把目标锁死在「已首推」状态。
- SKU 与门店均**必填**；空配置不再当作「全部推送」。

> 已知安全项：`order_url` 是用户可填的任意跳转目标，见 `SECURITY.md`。

---

## 7. 可观测性

上次事故的教训是「Watcher 几个月没取到数据、进程却一直健在」。所以自愈逻辑之外，
**看不见的失效仍等于没修** —— 这里有三层独立的可见性：

| 层 | 内容 |
|---|---|
| `data/health.json` | 每轮写一次（**只有 supervisor 写**） |
| `main.py health --max-age 900` | 按「上次成功取数」新鲜度 exit 0/1，供 docker healthcheck |
| Bark 告警 | 连续失败达 3 次推送，恢复后推「已恢复正常」 |

`health.json` 字段：`last_attempt_at` / `last_success_at` / `last_success_epoch` /
`consecutive_failures` / `last_error_kind` / `last_error` / `alerted` / `last_alert_epoch` /
`round_ms` / `profile_bytes` / `disk_free_mb`。

告警的两个细节（都踩过）：

- **阈值与节流**：连续失败 ≥3 次才发；同一轮故障 30 分钟内只发一次。
- **告警状态要跨重启存活**：「已告警」(`alerted`) 与「上次告警时刻」(`last_alert_epoch`) 都要落盘。
  只恢复其中一个都不行 —— 缺 `alerted` 则恢复时发不出「已恢复正常」；
  缺 `last_alert_epoch` 则重启后节流判断必然成立，30 分钟窗口内会重复告警。
  主循环是先 `record()`（写盘用的是改动前的值）再 `maybe_alert()`（这才改它们），
  所以 `maybe_alert` 改完必须**立刻 patch 式补写**这两个字段，否则要等下一轮才落盘。

界面侧：`GET /api/health` 只读展示（不判断健康），配置页顶部显示上次成功检查时间、距今多久、
本轮耗时、profile 体积、剩余磁盘、连续失败次数与最后错误。页面**打开即加载**一次，
之后每 60 秒自动刷新。注意 `last_success_epoch` 是**秒级**时间戳，前端展示前要先换算成分钟/小时
（否则会把秒当成分钟，数值大 60 倍）。

---

## 8. 部署形态

两个服务共用一份镜像（`python:3.13-slim` + apt chromium + pip playwright），
`docker-compose.yml` 里有几项是**为上面的机制服务的**，不是随手加的：

| 配置 | 为什么 |
|---|---|
| `hostname: astore-watcher` | 让陈旧 SingletonLock 能自愈（见 5.6） |
| `shm_size: 2gb` | Chromium 默认 `/dev/shm` 只有 64MB，不加会随机崩溃 |
| `stop_grace_period: 60s` | 配合「收到 SIGTERM 立刻中断 worker」，让 chromium 有时间干净释放锁 |
| watcher 的 healthcheck | 按 `main.py health --max-age 900` 判；**web 不加**（它没有浏览器） |
| `./data` 挂载 | profile、配置、状态、健康信息都在这里，web 与 watcher 共享 |

具体怎么构建、启动、配置、看日志，见 `README.md`。
