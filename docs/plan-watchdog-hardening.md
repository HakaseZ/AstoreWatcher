# Watcher 浏览器健壮性加固（子进程隔离 + 健康检查）

## 背景与根因

生产服务器上容器曾被非正常终止，`profile/SingletonLock` 残留在宿主机 `./data`。
容器 hostname 默认为容器短 ID，重建后 hostname 变化 → Chromium 判定"锁被另一台电脑持有"，
**直接 exit 21，不检查那个 pid 是否存活** → 永久锁死。
`poller.py:240` 的 `except Exception` 吞掉异常，进程假装健康地空转数月，无人知晓。

**核心教训**：缺的不是自愈逻辑，而是**可观测性**。所以 D 层（健康检查+告警）是本方案的验收前提，
不做 D，A–C 修得再好也算失败。

---

## 架构变更：supervisor + worker

当前 `poller` 进程同时负责「驱动浏览器」和「业务决策」。改为：

```
poller (supervisor, 不 import playwright)
  └─ spawn python3 worker.py --spec <json> --out <json>
       └─ 起浏览器 → 预检 → 取数 → 写 out → exit(code)
```

**为什么用子进程**：把「浏览器可能出的任何毛病」压缩成统一路径 *worker 退出 → 父进程重启重跑*。
清单里的失败模式只是已知部分，子进程隔离对**未知**失败模式同样提供兜底。
`page.evaluate`（`browser.py:152`）在 sync API 下不受任何 timeout 约束，一旦挂死会永久阻塞，
只有 OS 级 killpg 能打断；watchdog 线程的 `ctx.close()` 走 CDP 通道，卡在底层时送不进去。

**为什么 IPC 用 JSON 临时文件而非 stdout**：挂死时 worker 根本不会产生 stdout 输出，
父进程无法区分"挂了"和"输出为空"；stdout 还有缓冲、部分写、半截 JSON 的问题。
落盘 + 原子改名天然可判定：**out 文件不存在 = worker 没跑完**。
worker 的 stdout/stderr 直接继承父进程，日志进 docker log，零冲突。

### worker 契约

- 调用：`python3 worker.py --spec <path> --out <path>`
- spec：`{profile, location, parts, executable}`
- out：`{ok, checked_at, snapshot, failed, kind, stderr_tail}`
- **`checked_at` 必须由 worker 在取数完成时计算**（不是父进程），这样同一份 snapshot 分发给
  多个 target 时，十几秒间隔内它们的检查时刻是同一个。
- **worker 绝不写 state.json / init.json / health.json** —— 全部由父进程写。
  被 SIGKILL 的 worker 不可能留下半截状态或错误地把 init 标记写死。

### exit code

| code | 含义 | 父进程动作 |
|---|---|---|
| 0 | 成功（failed 可能非空） | 正常推送 |
| 3 | 部分批次失败但 snapshot 非空 | 正常推送 + warn |
| 10 | 陈旧 SingletonLock（hostname≠当前 且 pid 已死） | 清锁后重试 1 次 |
| 11 | 存在**活着的**孤儿 chromium 持有 profile | **不重试、不杀、立即告警** |
| 12 | profile 损坏（LevelDB 特征） | 走 rebuild 预算；无预算则告警 |
| 14 | 预检失败（可执行缺失/不可写/磁盘满） | 立即告警，不重试 |
| 15 | 取数中浏览器崩溃（Target crashed/closed） | 重开浏览器重试 1 次 |
| 9 | worker 自身未捕获异常 | 重试 1 次后告警 |

除 0/3 外，worker 必须写出 out 文件（带 `kind` / `stderr_tail`）。

---

## 各失败模式的对策与落点

### A 启动前

| # | 对策 | 落点 |
|---|---|---|
| A1 | 陈旧锁判定：读 `SingletonLock` symlink（`<hostname>-<pid>`），hostname 不匹配且无活孤儿 → 删三个 Singleton* 文件 | `worker.py` |
| A1' | **`hostname: astore-watcher` 固定容器 hostname** —— Chromium 是「先比 hostname，相同才验 pid」，hostname 固定后绝大多数陈旧锁会被 Chromium 自己识别 pid 已死而**自动接管**，天然自愈 | `docker-compose.yml` |
| A2 | 扫 `/proc/*/cmdline` 找含 `--user-data-dir=<profile>` 的进程（比读锁更可靠）→ exit 11 告警，**不擅自杀**（避免误伤手动 `once` 排障） | `worker.py` |
| A3 | launch 失败且 stderr 含 LevelDB 特征 → exit 12 | `worker.py` |
| A4 | profile 目录 makedirs + 写权限预检 | `worker.py` |
| A5 | `shutil.which` 预检可执行文件。**同时修 `browser.py:53` 的 `"150.0.0.0"` 静默 fallback** —— 它掩盖真实的执行文件问题，还会让 UA 与实际版本不符（更容易被 shield 判机器人），改为 raise | `browser.py:43-53` |
| A6 | `shutil.disk_usage` 低于 200MB → 告警 | `worker.py` |
| A7 | profile 目录大小记入 health.json，超 500MB 告警 | `worker.py` |

### B 运行期

| # | 对策 | 落点 |
|---|---|---|
| B1/B3 | 崩溃 → exit 15 → 父进程重开浏览器重试 1 次（**此前完全无重启重试，整轮作废**） | `worker.py` + `supervisor.py` |
| B2 | 单轮硬超时 **225s**：父进程 `join(225)`，超时 killpg。推导：启动+首开页 75s + 3 批取数含各一次 reheat 135s + 收尾 15s。正常轮次 30-60s，不会误杀 | `supervisor.py` |
| B5 | 每批 query 前检查 `page.is_closed()`，失效则重建 page + 重开购买页 | `browser.py:165` |

### C 退出期

| # | 对策 | 落点 |
|---|---|---|
| C1 | `stop_grace_period: 60s`（覆盖 Docker 默认 10s）。**配合下面这条才闭环** | `docker-compose.yml` |
| C1' | 父进程 SIGTERM handler **立即中断 worker 进程组**并退出，不等这一轮跑完。这是能让 60s 宽限期成立的前提，代价是当前这一轮数据丢弃（下一轮 1-2 分钟内补上，对库存监测无实质影响） | `poller.py:211` handler |
| C2 | `start_new_session=True` + `os.killpg` → chromium 随 worker 进程组一起死。kill 顺序：**SIGTERM → join(5) → SIGKILL**（让 playwright 干净释放锁并 flush LevelDB；直接 KILL 会制造新的 A1/A3；但必须限时兜底，因为挂死的 chromium 不响应 TERM） | `supervisor.py` |

### D 可观测（验收前提）

| # | 对策 | 落点 |
|---|---|---|
| D1 | `config.json` 顶层新增 **`health_webhook`**（独立 Bark 地址，与目标推送解耦）。连续失败 ≥3 次触发告警，节流 30 分钟一条；恢复后推一条「已恢复」。告警自身异常绝不能冒泡到 loop。**未配置时的行为：仅配置页显示「未配置告警」警示，不做任何降级**——不复用 target 的 bark_url，也不额外刷日志（已确认） | `config.py` / `supervisor.py` |
| D2 | 每轮写 `data/health.json`：`last_attempt_at` / `last_success_at` / `last_success_epoch` / `consecutive_failures` / `last_error` / `last_error_kind` / `round_ms` / `profile_bytes` / `disk_free_mb` | `supervisor.py` |
| D3 | `main.py health` 子命令按 health.json 新鲜度 exit 0/1；docker-compose 给 watcher 加 healthcheck（interval 60s / retries 3 / start_period 120s）。**web 服务不加**——它没有浏览器 | `main.py` / `docker-compose.yml` |
| D4 | `GET /api/health` + 配置页显示「上次成功检查时间 / 连续失败次数 / 最后错误」 | `web.py` / `static/index.html` |

---

## rebuild_profile 阶梯（护栏）

profile 里的 shield cookie 很珍贵，删了可能被判机器人持续 541，必须是最后手段：

1. 现有 `poller.py:122`「全批失败立刻 rebuild」**弱化**为：连续 **3 轮**全部失败且预算允许才 rebuild，
   否则一次接口抖动就丢 cookie。
2. 预算：`data/profile_rebuild.json` 记 `{date, count}`，**每 24h 上限 1 次，同一轮内绝不超过 1 次**。
3. `browser.py:192` 的 `rmtree` 改为**重命名备份**到 `profile.corrupt-<ts>`，留人工捞回 cookie 的机会。

---

## 新增需求：推送带检查时间

- `notifier.py:31` `send(..., checked_at=None)`，body 末尾追加 `\n檢查時間：<YYYY-MM-DD HH:MM>`；
  `push_target` 透传。
- 放在**最终发送层**，不塞进 `format_snapshot` / `build_messages` 这两个纯文案函数。
- **因此 `tests/test_notifier.py` 一行都不用改**：该文件 7 处 `body` 严格相等断言
  （:126 :134 :139 :398 :425 :430 :435，含 3 处 `body == ""`）和 :331 的 `split("\n")` 长度断言，
  打的都是纯文案函数的返回值，`checked_at=None` 时 `send` 对 body 零改动。
- 时区公共落点：`config.py` 新增 `HK_TZ` / `hk_now()`（`web.py:49` 改为 re-export 同一常量）。
  worker 只 import config，不碰 FastAPI。
- **注意 `web.py:168` 的 `push_cached_first_full`**：它走 `bark_post` 且用的是上一轮缓存快照，
  语义上不能说"本轮检查"，改为带 state 里落盘的 `checked_at` 并标注「上次檢查時間」。

---

## 文件改动清单

| 文件 | 增/改 | 内容 |
|---|---|---|
| `worker.py` | **新增** | 子进程入口、预检、锁判定、/proc 孤儿扫描、异常分类、写 out JSON（含 checked_at） |
| `supervisor.py` | **新增** | spawn(start_new_session)、join/killpg、重试阶梯、rebuild 预算、health.json 写、告警发送 |
| `poller.py` | 改 | 拆 `_fetch_snapshot` / `_dispatch`；用 supervisor 替换 BrowserSession；SIGTERM 转发；结构化异常处理 + 失败计数；传 checked_at |
| `browser.py` | 改 | `chromium_version` 不再静默 fallback（A5）；每批前检查 `page.is_closed()`（B5）；rmtree → 重命名备份 |
| `config.py` | 改 | 新增 `HK_TZ` / `hk_now()`；`default_config` 与 `validate` 白名单加 `health_webhook` |
| `notifier.py` | 改 | `send` / `push_target` 支持 `checked_at` |
| `main.py` | 改 | `cmd_once` 改走 supervisor（**保证 once 与生产同一条路径**）；新增 `health` 子命令 |
| `web.py` | 改 | `HK_TZ` 改从 config re-export；新增 `GET /api/health` |
| `static/index.html` | 改 | 顶层加「健康告警 Bark 地址」输入框 + 同步逻辑 + 健康状态显示 |
| `docker-compose.yml` | 改 | watcher 加 `hostname: astore-watcher`、`stop_grace_period: 60s`、healthcheck |
| `Dockerfile` | 改 | COPY 加 `worker.py supervisor.py` |
| `config.example.json` | 改 | 顶层加 `health_webhook` |
| `tests/test_poller_edge.py` | 改 | 删掉 `types.ModuleType("browser")` 假模块那一套（poller 不再 import browser），改为 patch `_fetch_snapshot`；顺手修 `:87` 的全局污染 |
| `tests/test_supervisor.py` | **新增** | 用假 worker 脚本覆盖 exit 10/11/12/14/15 与超时 killpg 路径 |

---

## 实施约束（必须遵守）

- **不碰 `data/` 下的任何实际文件**，尤其是本地那份真实的 Chromium profile 和 cookie。
  本地是开发环境，生产 profile 由用户自行处置（用户会整体重建生产环境，本方案无需考虑旧 profile 迁移）。
- **本地一律不启动真实浏览器**。所有验证用 `tests/test_supervisor.py` 的假 worker 脚本 +
  临时目录完成，不联网、不做真实 Chromeheadless profiling。因此 A3 的 LevelDB 特征串是
  **未经本机实测的推断**，落地时先在开发和生产环境的日志里确认真实 stderr 文本再放行 rebuild。
- 所有改动在 `feat/watchdog-hardening` 分支上进行，不停留在 main。

## 上线与验证

生产环境会整体重建，因此不涉及旧 profile 迁移或残留锁清理的前置操作。

1. **先在界面填好 `health_webhook`**（没有它就收不到任何告警，只能靠 docker healthcheck）。
2. 直接 `docker compose up -d`，观察首轮能否成功取到数据（`GET /api/health` 的 `last_success_at` 开始滚动）。
3. **验证告警**：人为制造失败（如临时挡住网络或 `docker compose exec watcher mv /usr/bin/chromium`），
   确认连续 3 轮失败后收到 Bark 告警，恢复后收到「已恢复」。
4. **验证优雅退出**：`time docker compose stop watcher`，应在数秒内干净退出；
   然后检查 `data/profile/` 里**没有残留 SingletonLock**（这是修复是否真正闭环的判据）。
5. `docker inspect` 看 watcher 健康状态是否为 healthy。
6. 界面保存一次配置后，确认 `health_webhook` **没有被清空**。
7. `hostname` 固定后人为留下陈旧锁复测：确认 Chromium 能自行接管（pid 已死时），不必依赖清理逻辑。

---

## 风险与回滚

1. **`start_new_session=True` 进程组归属**（最可能当场把服务打死）：
   必须确认 `os.getpgid(pid) == pid` 成立后 killpg 才安全，否则会打到父进程自己的组。
   缓解：worker 绝不写任何持久状态文件。
2. **rebuild_profile 误触发 → 永久 541**：若 LevelDB 特征串与实际 chromium 输出对不上，
   会出现每轮都 rebuild、不停丢 cookie 的死循环。缓解：先用**备份的** profile 副本在本机打出真实
   stderr 确认特征串，并把 24h 预算钉成硬闸。
3. **`health_webhook` 被静默清空**：`config.py:197` 的 validate 是白名单，
   `static/index.html` 的 PUT 是整份 state 原样回传 —— **前端不同步这个字段，用户一保存配置就把它抹掉**，
   而那恰好是告警最该响的时刻。必须改 index.html，且上线第一步就验证（见验证第 6 步）。
4. 回滚：切回 `origin/main`。

---

## 已确认决策

- 采用**子进程隔离**（非 watchdog 线程）
- 告警走**独立 `health_webhook`**，不复用 target 的 bark_url
- 范围：**A+B+C+D 全部**
- SIGTERM 到达 → **立即中断当前轮**，不等它跑完
- 单轮硬超时 **225s**
- 健康状态**做进 web 配置页**（含「未配置告警」警示）
- `health_webhook` 未配置时 → **仅配置页显示警示**，不做任何降级（不打日志、不复用 target）
- 故障恢复后 → **推一条「已恢复」通知**
- 旧生产 profile 不迁移：**用户会整体重建生产环境**，本方案按新 profile 冷启动设计
- 分支名：**`feat/watchdog-hardening`**（从 `origin/main` 拉出）
- 本计划文档以 `docs/plan-watchdog-hardening.md` 的形式随该分支提交，作为本轮的临时记录
