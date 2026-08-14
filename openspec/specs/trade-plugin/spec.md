# trade-plugin Specification

## Purpose
定义交易插件的独立行为契约：基于核心信号事件的实盘/模拟开仓、DBotX 服务端止盈止损任务、WS 成交结果记账、风险账本与管理员命令。插件自持全部 DBotX 凭证与配置。
## Requirements
### Requirement: 凭证与配置自治
交易插件 SHALL 自持 DBotX API Key、分链钱包 ID、Telegram 命令配置等全部运行参数。核心 MUST NOT 持有或注入这些凭证。插件缺失凭证时 MUST 进入禁用状态并告警，不影响核心信号推送。

#### Scenario: 凭证缺失
- **WHEN** 交易插件启动时未配置 DBOTX_API_KEY
- **THEN** 插件进入禁用状态并告警，核心继续正常产生与推送信号

### Requirement: 信号驱动的开仓
交易插件 SHALL 订阅 `signal_created` 事件；在信号有效期内、风险账本放行且插件处于启用模式（PAPER/LIVE）时创建开仓。同一 `chain + token_address + position_mode` 存在未终态仓位或未完成买单时 MUST 拒绝开仓（无自动加仓）。LIVE 开仓 MUST 使用服务端止盈/止损组任务（TP/SL 组、追踪止损组、过期执行策略），禁止插件本地轮询行情模拟止盈止损。

#### Scenario: 重复信号拒绝加仓
- **WHEN** 插件收到第二条同链同币同模式的 `signal_created`
- **THEN** 插件拒绝开仓并记录原因，不创建第二笔买单

#### Scenario: 服务端止盈止损附着
- **WHEN** 插件在 LIVE 模式创建一笔买入任务
- **THEN** 请求显式包含服务端止盈/止损组参数（stopEarnGroup/stopLossGroup 或等价单值），且插件本地不启动任何行情轮询模拟止盈止损

### Requirement: PAPER 直连 Simulator
PAPER 模式 SHALL 直接调用 DBotX Simulator 创建订单（含同款服务端 TP/SL 组），以 Simulator 账户汇总与 WS 成交结果作为模拟事实。插件 MUST NOT 维护本地 1m 成交模型或人工对账命令。

#### Scenario: 模拟开仓
- **WHEN** 插件处于 PAPER 模式并收到有效信号
- **THEN** 调用 Simulator 创建带 TP/SL 组的买入任务，后续状态由 Simulator WS 结果与账户汇总驱动

### Requirement: WS 结果记账
交易插件 SHALL 订阅 DBotX `subscribeTradeResults` 推送（买卖/止盈/止损/追踪止损的成功与失败事件），以事件更新本地仓位与订单状态。插件 MUST 保留兜底查询：重启恢复时用批量订单查询（`swap_orders`）与钱包资产（`/kline/wallet/assets`）重建状态。本地记录与 DBotX 事实不一致时 MUST 以 DBotX 事实为准并记录差异。WS 事件未达且批量查询无结果时，订单 MUST 标记为待确认并向管理员告警，MUST NOT 自动重发。插件使用的 DBotX 数据 API（钱包资产等）额度消耗 MUST 由插件自行管理与兜底，核心无感知。

#### Scenario: 重启恢复状态
- **WHEN** 插件重启时存在未终态本地订单
- **THEN** 插件通过批量订单查询与钱包资产快照重建仓位状态，与远端一致后恢复记账

### Requirement: 风险账本
交易插件 SHALL 维护按 PAPER/LIVE 隔离的风险账本：全局最大敞口、单币最大仓位、单链最大敞口、UTC 日亏损上限、连续亏损上限与冷却、最大同时持仓数。风险额度 MUST 在创建买单的同一事务内检查并预留。风险事件 MUST 通过核心通知 API 上报管理员。

#### Scenario: 触及日亏损上限
- **WHEN** 某链某模式的已实现日亏损达到上限
- **THEN** 该链该模式在下一个 UTC 自然日前拒绝新开仓，已有仓位的退出不受影响

### Requirement: 交易命令
交易插件 SHALL 通过核心命令路由提供：`/mode paper|live`（仅影响新开仓）、`/positions`、`/closeall`（仅处置插件跟踪仓位）。高风险命令 MUST 经过核心统一二次确认。插件 MUST NOT 触碰不在其账本内的余额。

#### Scenario: 模式切换
- **WHEN** 管理员确认执行 `/mode live`
- **THEN** 插件验证实盘钱包存在且链类型匹配后，后续新开仓走 LIVE；已有 PAPER 仓位保持原模式

