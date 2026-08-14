# 任务：实施信号引擎与插件

三阶段串行交付。**检查点：上一阶段验收通过前，下一阶段任务不得开工。**

## 第一阶段：核心信号引擎（含插件总线，SDK 规范行为全部落地）

### 1. 项目骨架与配置

- [x] 1.1 建立 `app/core/` 目录骨架、依赖清单（httpx/websockets/python-telegram-bot/aiosqlite/pydantic/pytest）与 Dockerfile
- [x] 1.2 实现 `config.py`：环境变量加载（CoinGecko Key、Telegram Token/管理员 ID/频道 ID）、日志配置（凭证脱敏）
- [x] 1.3 实现 `config/strategy.yaml` Pydantic 严格 Schema（文档第 6 章全部字段；拒绝执行/风险/晋升字段）+ canonical JSON 与 `strategy_hash` 工具
- [x] 1.4 实现 `config/plugins.yaml` 解析（enabled 列表）

### 2. 存储层

- [x] 2.1 迁移框架与 `001_init.sql`（文档第 9.3 节全部核心表、索引、唯一约束；`wallet_*` 聪明钱表 v1 建表不写入，v2 启用）
- [x] 2.2 `storage_task` 优先级队列写入（策略事务 > 信号与安全快照 > 候选行情）、队列水位暂停规则、1m 行情内存合并
- [x] 2.3 Repository 层基础 CRUD 与备份（SQLite Backup API）

### 3. 供应商 clients

- [x] 3.1 CoinGecko REST client（httpx、错误分类、速率处理）与归一化模型
- [x] 3.2 首次能力探测报告（带 Python 版本/脚本版本/UTC 元数据；含 G2 逐条计费契约探测）与契约测试 fixture 机制
- [x] 3.3 GoPlus client（无 Authorization、30 次/分钟限流）与双链字段归一化（文档第 5.4/5.6 节映射）
- [x] 3.4 `/key` client 与用量告警/展示数据（文档第 4.6 节简化版）

### 4. 发现与候选流水线

- [x] 4.1 `discovery_task`：四个查询模板调度（独立 `next_due_at`）、Megafilter 参数注入、三标签判定（文档第 4.2/4.3 节）
- [x] 4.2 候选依赖图：Token Info → Multiple Pools → Tokens Multi → Top Holders → GoPlus PRE_MONITOR_CHECK → G3（文档第 4.4 节）；筛选前特征与逐池决策特征写入 `discovery_snapshots`
- [x] 4.3 池选择屏障与决定池选择（trade_allowed > 总分 > 流动性 > 成交量 > 地址）
- [x] 4.4 安全门禁状态机（SAFE/RISK/UNKNOWN/NOT_APPLICABLE/STALE，文档第 5 章）

### 5. 实时行情（WebSocket）

- [x] 5.1 G1/G3 ActionCable client（订阅/确认契约、双链独立连接）
- [x] 5.2 断线退避/抖动/重连与鉴权失败停止；重连后订阅重建
- [x] 5.3 G3 1 秒 K 线聚合（final 判定、W5/W10 窗口、缺口拒绝）与 G1 顶部池映射
- [x] 5.4 订阅准入：100 物理上限 + WS 每日预算（`daily_credit_budget`）+ 候选淘汰

### 6. 评分、防追高与信号

- [x] 6.1 评分纯函数库：全部派生特征公式（文档第 6.5 节）、权重/缺失动作/方向校验
- [x] 6.2 聪明钱 v1：Top Traders + Pool Trades 取证、TOKEN_PROFITABLE 分类与卖出阻挡（LOCAL_SMART 权重固定 `"0"`）
- [x] 6.3 防追高状态机（垂直上涨/VWAP 偏离、高位整理、回踩恢复，文档第 6.6 节）
- [x] 6.4 候选状态机（WATCHING/SETUP/SIGNAL/REJECTED/EXPIRED）与 `signal_generation` 去重/重新武装
- [x] 6.5 信号落库（决策快照完整特征向量）、`signal_created`/`signal_invalidated` 事件（Schema v1，文档第 15.2 节）；落库前执行 GoPlus PRE_EXECUTION_CHECK（120 秒快照）

### 7. 插件总线（SDK 规范第 2-7 章行为的核心侧实现）

- [x] 7.1 事件分发：内存队列 fan-out、分发超时（默认 5 秒）、熔断滑动窗口计数与告警、重启补发（至少一次投递）
- [x] 7.2 插件加载：`plugins/<name>/register.py` 约定、`plugins.yaml` 启用顺序、加载失败隔离、同步（阻塞式）handler 注册拒绝
- [x] 7.3 通知 API（outbox 提交、delivery_key 返回与状态查询）与命令路由（冲突拒绝、管理员校验、确认框架接入）
- [x] 7.4 插件存储接口与独立迁移执行（`plugins/<name>/migrations/`，SDK 规范第 6 章）
- [x] 7.5 总线测试：至少一次投递幂等、分发超时、加载失败隔离、命令冲突拒绝、熔断与解除

### 8. Telegram

- [x] 8.1 Long Polling：`telegram_updates` offset 持久化与恢复、update 去重
- [x] 8.2 `telegram_outbox`：delivery_key 幂等、PENDING 补发、SENDING→DELIVERY_UNKNOWN 恢复、过期信号"仅通知"标记
- [x] 8.3 一次性确认 nonce（60 秒、绑定参数、同事务消费）与核心命令集（文档第 8.2 节）

### 9. 结果追踪与用量控制

- [x] 9.1 结果标签追踪：15m/1h/6h/24h 定点 Multiple Pools 快照 + 补洞一次 + 缺口记录
- [x] 9.2 spike：`simple token_price` 契约验证报告，通过后切换结果追踪数据源
- [x] 9.3 REST 全局并发上限（`rest_concurrency_max`）与关键事务优先

### 10. 回测与策略激活

- [ ] 10.1 回测数据准备：决策快照/钱包观察/结果覆盖率检查、OHLCV 回补计划与管理员确认（`before_timestamp` UTC 秒分页）
- [ ] 10.2 1m 成交模型纯函数实现（`NOT_FILLED/DATA_GAP`、synthetic 隔离、冲击公式）
- [ ] 10.3 回测报告（文档第 10.5 节指标全集）与 run_id 确定性
- [ ] 10.4 手动激活/回退：Schema 校验 → 报告 → 确认 → ACTIVE 快照切换；`validate-collection` dry-run
- [ ] 10.5 基准 `strategy.yaml` 交付（双链完整值）

### 11. 故障恢复与第一阶段验收

- [ ] 11.1 启动恢复：outbox 未终态、结果追踪计划、今日 WS 计数、订阅重建、插件加载
- [ ] 11.2 容量熔断（70/80/90）与数据保留清理任务（文档第 9.4/9.5 节）
- [ ] 11.3 单元测试覆盖文档第 13.1 节全部条目
- [ ] 11.4 集成测试覆盖文档第 13.2 节核心条目（供应商契约 fixture、WS 断线注入、Telegram 回放、SQLite 恢复）
- [ ] 11.5 第一阶段验收：文档第 13.3 节 20 条核销 + `signal-core`/`plugin-sdk` spec 场景核销 + 与用户共同确认

## 第二阶段：交易插件

- [ ] 12.1 DBotX 契约测试（交易插件文档第 10 章待测项：Simulator list_tasks、kline 字段单位、WS 事件字段）
- [ ] 12.2 凭证与环境加载、模式状态、钱包验证（`account/wallets`）
- [ ] 12.3 DBotX client：`swap_order`/`sim_swap_order` 参数映射（含服务端 TP/SL 组、滑点三档、费用字段）
- [ ] 12.4 WS `subscribeTradeResults`：订阅/心跳/重连/事件幂等（远端订单 ID）
- [ ] 12.5 对账与恢复：`swap_orders` 批量查询、`/kline/wallet/assets`、`account/swap_trades`、重启恢复顺序；DBotX 数据 API 额度不足时对账轮询频率降级
- [ ] 12.6 简化状态机（PENDING/SUBMITTED/DONE/FAILED/UNCONFIRMED、OPEN/CLOSING/CLOSED）与开仓前置检查
- [ ] 12.7 风险账本（敞口/日亏损/连续亏损/冷却/最大持仓数，事务预留，PAPER/LIVE 隔离）
- [ ] 12.8 Telegram 命令：`/mode` `/positions` `/closeall` `/risk`
- [ ] 12.9 第二阶段验收：`trade-plugin` spec 场景核销 + 交易插件文档验收项 + 与用户共同确认

## 第三阶段：波段插件

- [ ] 13.1 DBotX 契约测试（波段文档第 7 章待测项：kline 分页/单位、Pair Updates WS、Limit Order 行为、Pair Info 安全字段）
- [ ] 13.2 行情模块：`/kline/chart` 分页拉取、Pair Updates WS 订阅、Pair Info 安全检查
- [ ] 13.3 服务端触发映射：Limit Order `settings[]` 创建/取消/替换（triggerPriceUsd/triggerDirection/useMidPrice/expireExecute）
- [ ] 13.4 箱体判定与循环状态机（CANDIDATE/WAIT_BUY/HOLDING/WAIT_SELL/TERMINATED、WS 确认循环）
- [ ] 13.5 终止条件（最大轮数/箱体破坏/流动性崩塌/安全恶化/管理员命令）与资金隔离记账（`swing_*` 表）
- [ ] 13.6 第三阶段验收：`swing-plugin` spec 场景核销 + 与用户共同确认

## 14. 最终验收

- [ ] 14.1 全部 spec 场景与文档验收条件核销记录
- [ ] 14.2 `openspec validate --strict` 通过；供应商契约测试全部绿灯
- [ ] 14.3 运行验收：单进程、单 strategy.yaml、单 bot.sqlite 验证
