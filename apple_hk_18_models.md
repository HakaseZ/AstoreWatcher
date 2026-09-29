# 港版 iPhone 18 系列型号 / 颜色 / 容量（Apple 香港 hk-zh）

> 数据来源：`apple.com/hk-zh` 购买页内嵌 SKU + `fulfillment-messages` 接口的 `storePickupProductTitle`
> 标准版 `iphone-18` 购买页为 404，港版 18 系列仅有 **Pro** 与 **Pro Max** 两款。
> 颜色共 4 种：布根地紅色 / 冰川色 / 銀色 / 黑色；容量共 4 档：256GB / 512GB / 1TB / 2TB。

## iPhone 18 Pro Max（16 个 SKU）

| 颜色 | 256GB | 512GB | 1TB | 2TB |
|---|---|---|---|---|
| 布根地紅色 | MJXQ4ZA/A | **MJXV4ZA/A** | MJY04ZA/A | MJY44ZA/A |
| 冰川色 | MJXR4ZA/A | MJXW4ZA/A | MJY14ZA/A | MJY54ZA/A |
| 銀色 | MJXP4ZA/A | MJXU4ZA/A | MJXY4ZA/A | MJY34ZA/A |
| 黑色 | MJXN4ZA/A | MJXT4ZA/A | MJXX4ZA/A | MJY24ZA/A |

## iPhone 18 Pro（16 个 SKU）

| 颜色 | 256GB | 512GB | 1TB | 2TB |
|---|---|---|---|---|
| 黑色 | MJRP4ZA/A | MJRU4ZA/A | MJRY4ZA/A | MJT34ZA/A |
| 銀色 | MJRQ4ZA/A | MJRV4ZA/A | MJT04ZA/A | MJT44ZA/A |
| 布根地紅色 | MJRR4ZA/A | MJRW4ZA/A | MJT14ZA/A | MJT54ZA/A |
| 冰川色 | MJRT4ZA/A | MJRX4ZA/A | MJT24ZA/A | MJT64ZA/A |

## 说明
- `MJXV4ZA/A` 即用户示例，对应 **iPhone 18 Pro Max 512GB 布根地紅色**。
- 库存查询接口（仅状态，无精确数量）：
  `https://www.apple.com/hk-zh/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&mts.1=compact&parts.0=<PART>&location=<地點繁體，如 中環>`
- 多 part 一次查询需分批（单批 ≤15 个），否则 Apple 会截断丢弃部分 part。

## 查询前置条件（硬约束，缺一不可）

要成功拉到香港门店取货数据，必须同时满足以下 5 个条件（除标注外均已实测）：

### 1. 域名必须用 `apple.com/hk-zh`
```
https://www.apple.com/hk-zh/shop/fulfillment-messages
```
**注意：`apple.com.hk` 连不上**（该域名在网络层就不通，报 `ERR_CONNECTION_CLOSED`）。香港站点挂在 `apple.com` 的 `/hk-zh` 路径下，这一点之前排查走过弯路。

### 2. 请求必须由「真实 Chrome」发出 —— Akamai JS 挑战
Apple 香港站部署了 Akamai Bot Manager：会先下发一段 JS 挑战，浏览器执行完才下发反爬 cookie。实测四种客户端：

| 客户端 | 结果 | 原因 |
|---|---|---|
| curl（裸） | HTTP 541 拦截页 | TLS/浏览器指纹不符 |
| curl + 完整浏览器 cookie（1538 字符） | HTTP 541 | 在到看 cookie 之前就被拦 |
| `curl_cffi` `impersonate='chrome'` | HTTP 541 | 仿了 TLS 指纹，但没有 JS 环境跑挑战 |
| **真实 Chrome 会话内 `fetch`** | ✅ HTTP 200 | 真的执行了 JS 挑战、拿到了 cookie |

结论：**必须有一个真实浏览器引擎**，纯命令行 HTTP 库这条路走不通。

### 3. 必须在 `apple.com/hk-zh` 页面上下文里 fetch
即使浏览器开着，若当前活动标签页不在 Apple 域上，同样的 fetch 会报 `Failed to fetch`（跨源、不带会话 cookie）。正确姿势是**在已打开 HK 购买页的标签页内**执行：
```js
fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
```
推荐先打开：`https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro`

### 4. 必须有 Akamai 反爬 cookie（`_abck` / `ak_bmsc`）
这两个 cookie 只有在真实浏览器访问过 Apple 香港页面、跑完 JS 挑战后才下发，**无法手工构造**。
- **首次准备**：先在 Chrome 里打开上述购买页 → 自动获得；
- **失效处理**：cookie 会过期，过期后再请求会回到 541 或空数据 → **重新打开一次购买页即可续命**（轮询脚本需内置这个逻辑）。

### 5. 参数约束
- `parts.N` 索引从 0 递增，**单批 ≤15 个**；32 个 SKU 要拆 3 批（15 / 15 / 2），超过 Apple 会**静默截断**丢数据，不报错；
- `location` 用**香港繁体**地名并 URL 编码，如 `中環`（返回 6 家门店）；
- 必须带 `fae=true&pl=true&mts.0=regular&mts.1=compact`。

### 返回值限制（重要）
这套接口**只有状态，没有数量**。`partsAvailability` 里可用字段：
- `pickupDisplay`：`available` / `unavailable`
- `pickupSearchQuote`：`今天可取貨` / `暫無供應`
- `messageTypes.regular.storePickupQuote`

没有任何 `quantity` / `availableQuantity` / `inventory` 字段 —— Apple 不公开精确库存台数。
> 待办：此前怀疑存在返回数量的 availability 接口，但 `/retail/_store/availability` 与 `/shop/availability` 实测均 **404**。你提到「盯果」能显示库存数量，若能再把该站点链接发我，可直接扒它调用的真实接口来定位。（**未解决**）

### 当前本机零安装满足条件的方式
用已有的 `agent-browser` + 其拉起的 9222 端口 Chrome：
```bash
agent-browser connect 9222
agent-browser open "https://www.apple.com/hk-zh/shop/buy-iphone/iphone-18-pro"
agent-browser eval "<在该页面上下文跑 fetch>"
```
注意：`agent-browser eval` 返回的是**双重 JSON 字符串**（外层多套一层引号），解析需 `json.loads` 两次。

### 尚未验证 / 仅为建议
- **轮询安全间隔与是否限流：尚未压力测试**，建议 ≥60s 并加随机抖动（待实测确认）；
- 若 9222 那个 Chrome 窗口关闭，轮询即失效（cookie 随浏览器进程消亡）。
