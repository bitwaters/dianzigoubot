# SOL/BSC 潜力币信号引擎——交易插件文档

| 项目 | 规范 |
|---|---|
| 文档性质 | 交易插件的独立设计、开发、测试与验收规范 |
| 上游契约 | 核心事件 Schema v1、插件 SDK 规范 |
| 执行供应商 | DBotX Trading API / Data API（官方文档：https://docs.dbotx.com） |
| 凭证 | DBotX API Key、分链专用钱包，全部由插件自持 |

---

## 1. 定位与边界

交易插件订阅核心 `signal_created` 事件，在插件自身的风控与模式（PAPER/LIVE）约束下，通过 DBotX 创建带服务端止盈/止损任务的买卖订单，订阅 DBotX WS 成交结果记账，维护自己的风险账本与 Telegram 命令。

- 插件不参与信号发现、评分与推送；核心不感知插件的订单与仓位。
- 插件只处置自己账本内的仓位与余额；不得触碰账本之外的余额。
- PAPER 直连 DBotX Simulator；LIVE 使用分链专用 DBotX 钱包。
- **插件不订阅 `signal_invalidated`**：信号作废或过期不影响已建立的仓位；仓位退出只由服务端止盈/止损任务、插件风控与管理员的退出命令决定。

## 2. 凭证与环境

运行环境必须提供（核心不配置、不注入）：

- `DBOTX_API_KEY`
- `DBOTX_SOLANA_WALLET_ID`、`DBOTX_BSC_WALLET_ID`
- `TRADE_MODE=paper|live`（重启后新开仓模式；已有仓位保持自身模式）

凭证缺失或钱包验证失败时插件进入禁用状态并告警，核心信号推送不受影响。LIVE 模式启动时和 `/mode live` 前调用 Wallet Info API 确认两个实盘 `walletId` 存在且链类型匹配；验证失败只拒绝 LIVE 新开仓，不影响 PAPER 与已有仓位退出。

## 3. 开仓前置检查

开仓前必须全部满足（在插件内执行，任一不满足即拒绝并记录原因）：

1. 事件为 `signal_created`，`schema_version` 受支持，`expires_at` 未过。
2. 事件 `telegram_status` 策略：默认要求投递状态不为 `FAILED/DELIVERY_UNKNOWN`（可配置放宽）。
3. 风控放行：第 8 章全部限额与冷却通过。
4. 模式与凭证：当前模式凭证可用（LIVE 钱包验证通过）。
5. 同一 `chain + token_address + position_mode` 不存在未终态仓位或未完成买单（无自动加仓）。
6. DBotX 供应商健康（最近一次调用无系统性错误）。

检查在同一 SQLite 事务内创建本地 PENDING 订单并预留风险额度；不满足时记录拒绝原因，不创建订单。

## 4. DBotX 交易契约

### 4.1 Fast Buy / Sell

- LIVE：`POST https://api-bot-v1.dbotx.com/automation/swap_order`
- PAPER：`POST https://api-bot-v1.dbotx.com/simulator/sim_swap_order`

参数映射：

| 请求字段 | 来源 | 说明 |
|---|---|---|
| `chain` | 事件 `chain` | `solana` / `bsc` |
| `pair` | 事件 `token_address` | 目标代币地址（DBotX 自行路由池，与核心决定池无关） |
| `walletId` | 分链钱包 ID | LIVE 必填；PAPER 固定 `""` |
| `type` | 插件意图 | `buy` / `sell` |
| `amountOrPercent` | 插件配置 | buy 为原生币金额（SOL/BNB）；sell 为比例 0–1，全部退出传 `1` |
| `maxSlippage` | 插件配置 | 0–1，第一版默认占位 `0.1`（待调参） |
| `priorityFee` | 插件配置 | Solana 十进制字符串；BSC 空字符串 |
| `gasFeeDelta`/`maxFeePerGas` | 插件配置 | EVM gwei，数值型 |
| `jitoEnabled`/`jitoTip` | 插件配置 | Solana 用；BSC 固定关闭/`0` |
| `customFeeAndTip`/`concurrentNodes`/`retries` | 插件配置 | `retries` 按 DBotX 原生语义（0–10），不再自建 transport 重试 |
| `migrateSellPercent` | 固定 `0` | 不创建开盘卖出任务 |
| `minDevSellPercent`/`devSellPercent` | 省略 | 不创建 Dev Sell 任务 |

滑点配置三档（插件配置，均为 0–1，调用前不变换单位）：

| 配置项 | 用途 | 映射 |
|---|---|---|
| `entry_slippage_pct_max` | 买入订单 | `maxSlippage` |
| `normal_exit_slippage_pct_max` | 普通卖出订单 | `maxSlippage` |
| `emergency_exit_slippage_pct_max` | `/closeall` 与安全恶化紧急卖出 | `maxSlippage` |

### 4.2 服务端止盈/止损组（buy 时附加）

买入请求必须显式附加服务端退出任务组（禁止插件本地轮询行情模拟止盈止损）：

| 请求字段 | 含义 | 第一版默认（占位，待回测调参后锁定） |
|---|---|---|
| `stopEarnGroup` | 止盈组，最多 6 档 | `[{"pricePercent": 0.5, "amountPercent": 1}]` |
| `stopLossGroup` | 止损组，最多 6 档 | `[{"pricePercent": 0.2, "amountPercent": 1}]` |
| `trailingStopGroup` | 追踪止损组，1 组 | 默认关闭；启用时 `activePricePercent` 必填 |
| `pnlOrderExpireDelta` | TP/SL 任务有效期（ms） | `43200000`（12 小时，占位） |
| `pnlOrderExpireExecute` | 过期是否自动执行 | `false`（占位） |
| `pnlOrderExpireExecuteSellAll` | 过期执行是否全卖 | `false`（占位） |
| `pnlOrderUseMidPrice` | Anti-Spike 中间价触发 | `false`（占位） |

`stopEarnGroup` 与 `stopEarnPercent` 同设时后者被忽略；插件固定使用 group 形式。服务端 TP/SL 触发由 DBotX 执行，插件经 WS 事件记账，不重复实现分档卖出。

## 5. WS 结果订阅

- 端点：`wss://api-bot-v1.dbotx.com/trade/ws/`，连接头带 `x-api-key`。
- 订阅消息：`{"method": "subscribeTradeResults", "tradeTypeFilter": [...]}`，过滤列表包含全部 `swap_*` 类型：`swap_buy_success`、`swap_buy_fail`、`swap_sell_success`、`swap_sell_fail`、`swap_take_profit_success`、`swap_take_profit_fail`、`swap_stop_loss_success`、`swap_stop_loss_fail`、`swap_trailing_stop_success`、`swap_trailing_stop_fail`。
- 心跳：至少每 60 秒一次（实现固定 30 秒 ping），超时断线自动重连。
- 幂等键：远端订单 ID；同一远端 ID 的重复事件只处理一次。
- 断线恢复：重连并重新订阅后，对未终态本地订单执行第 6 章兜底查询，与 WS 事件交叉去重。

## 6. 对账与恢复

| 接口 | 用途 | 成本 |
|---|---|---|
| `GET /automation/swap_orders?ids=` | 批量查询订单状态（init/processing/done/fail/expired、txPriceUsd、swapHash、errorCode） | 0 credits |
| `GET https://api-data-v1.dbotx.com/kline/wallet/assets?chain=&walletAddress=` | 钱包持仓与每币 `hold/cost/sold/pnl/pnlPercent/tokenBalanceUI` 对账 | 50 credits |
| `GET /account/swap_trades` | 已成交 fast buy/sell 记录 | 0 credits |
| `GET /account/wallets?type=` | 钱包存在性与链类型验证 | 0 credits |

- 本地记录与 DBotX 事实不一致时以 DBotX 为准并记录差异。
- 重启恢复顺序：加载未终态订单与仓位 → 批量查询远端状态 → 钱包资产快照重建仓位 → 订阅 WS → 恢复风险预留。
- WS 事件未达且批量查询无结果时，订单标记 `UNCONFIRMED` 并向管理员告警，禁止自动重发。
- 插件使用的 DBotX 数据 API 额度由插件自行管理与兜底（对账轮询频率随剩余额度降级），核心无感知。

## 7. 订单与仓位状态

订单状态：

```text
PENDING / SUBMITTED / DONE / FAILED / UNCONFIRMED
```

- 创建远端请求前持久化 `PENDING`；收到远端 ID 进入 `SUBMITTED`。
- WS 事件或批量查询确认成交为 `DONE`、明确失败为 `FAILED`；两者均无法确认时为 `UNCONFIRMED`（仅告警与人工处置，无自动冻结机）。
- 只有能证明请求未离开本机时才允许用原订单 ID 重新派发；其余情况禁止自动重试。

仓位状态：

```text
OPEN / CLOSING / CLOSED
```

- 仓位保存 `position_mode`、入场信号 ID、数量、`exit_pending` 与当前订单 ID。
- 止盈/止损/追踪止损由 DBotX 服务端任务触发，插件在收到对应 WS 事件后更新仓位为 `CLOSING`→`CLOSED`；普通卖出由插件创建 sell 订单。
- 服务端 TP/SL 任务失效或丢失时（WS 无事件且查询无任务），插件按第 6 章重建；确认丢单时经管理员确认补挂卖出。

## 8. 风险账本

以下限额按 `chain + PAPER/LIVE` 隔离：

- `max_total_exposure_usd`：跨链全局敞口上限，PAPER 与 LIVE 各自独立。
- `max_token_position_usd`、`max_chain_exposure_usd`：单币与单链上限。
- `chain_daily_loss_limit_usd`：UTC 自然日已实现净亏损上限，含费用/税费/滑点/网络费；达到上限后该链该模式在下一个 UTC 自然日前拒绝新开仓，已有仓位退出不受影响。
- `chain_consecutive_loss_limit` + `cooldown_seconds`：按同一链与模式的 CLOSED 仓位结算顺序统计，净盈利仓位清零计数；达到上限后执行一次 `cooldown_seconds` 冷却并将计数清零。相同时间按 `closed_at + position_id` 确定顺序。
- `max_chain_open_positions`：最大同时持仓数。
- `order_size_native`：单笔买入原生币金额。

敞口包含 OPEN/CLOSING 仓位与 PENDING/SUBMITTED/UNCONFIRMED 买单预留；退出中的仓位在确认 DONE 前不释放额度。所有限额在创建买单的同一 SQLite 事务内检查并预留，防止并发信号超额。风险事件经核心通知 API 上报管理员。

## 9. Telegram 命令

经核心命令路由注册：

- `/mode paper|live`（二次确认）：切换新开仓模式；已有仓位保持原模式。
- `/positions`：当前 PAPER/LIVE 仓位、未终态订单与敞口。
- `/closeall`（二次确认）：对插件账本内全部仓位创建紧急全部卖出（紧急滑点上限）；只处置已跟踪仓位。
- `/risk`：风险账本状态（敞口、日亏损、连续亏损、冷却）。

## 10. 待契约测试项

以下 DBotX 能力仅有官方文档代码示例证据，实现前必须先做契约测试锁定：

- Simulator 订单状态查询（代码示例中的 `/simulator/list_tasks` 未在接口总表列出）。
- `/kline/chart` 的 volume 单位与 `end` 参数分页边界。
- `subscribeTradeResults` 各类事件的实际字段结构。
- `wallet/assets` 的 pnl 字段在 Simulator 场景的适用性。

正式契约以实测为准，本文档与实现随测试结论更新。
