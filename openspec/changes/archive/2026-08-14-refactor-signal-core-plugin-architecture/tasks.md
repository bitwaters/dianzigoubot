# 任务：核心信号引擎 + 插件架构重构

交付分两个阶段串行执行，**检查点：第一阶段全部完成并通过验收前，第二阶段不得开工**。

## 第一阶段：核心信号引擎总控文档

- [x] 1.1 以现总控文档为底稿，产出逐节归属总表（保留/删除/改写/移入插件），与用户逐条确认后作为拆分依据
- [x] 1.2 改写第 1-3 章：文档控制规则、系统目标（删除交易目标）、技术架构（单进程 + 插件总线，删除 DBotX curl transport 规则）
- [x] 1.3 改写第 4 章：删除持仓监控降级与 `dynamic_position_reserve_credits`；修正 §4.4 Multiple Pools 契约（补充 buy/sell/net buy volume 字段与 `include_volume_breakdown` 关系）；新增结果标签追踪的 REST 轮询方案（默认 Multiple Pools，simple token_price 待验证优化）
- [x] 1.4 第 5 章安全门禁：内容保留，删除"开仓"语义改为"发信号"语义，PRE_EXECUTION_CHECK 保留为信号前置条件
- [x] 1.5 第 6 章策略字段：删除 `execution_model`/`execution`/`exits`/`risk`/`promotion_gates`；修正 §6.5 过时断言（标注 Multiple Pools 已提供买卖 USD 聚合字段，不新增特征公式）；更新 §6.5 特征公式表（仅删除已失效内容）
- [x] 1.6 第 7 章：仅保留候选状态（§7.1）与信号记录（§7.2 改写），删除订单/仓位状态机（§7.3-7.6）
- [x] 1.7 第 8 章：保留 Telegram 框架与投递幂等，删除交易命令（/positions、/orders、/closeall、/mode、/paper、/order resolve、/wallet 对账类），预留命令路由接口说明
- [x] 1.8 第 10 章：删除 `orders`/`positions` 表、`PROTECTED` 保护集与动态晋升样本管理；保留候选/信号/结果标签/回测相关表
- [x] 1.9 第 11 章：保留回测引擎（数据准备、1m 模型、结果标签、覆盖率、run_id），删除晋升门禁、70/30 样本外对比、采集 dry-run 激活路径；改写激活流程为手动确认
- [x] 1.10 第 12-15 章：故障安全表删除 DBotX 条目、恢复流程删除订单/仓位恢复；代码结构改为 app/core 布局；测试与验收清单对应删减；官方接口参考保留 CoinGecko/GoPlus 并更新链接
- [x] 1.11 定义插件总线事件 Schema v1（`signal_created`/`signal_invalidated` 载荷字段与版本规则），写入核心文档，供第二阶段 SDK 规范引用
- [x] 1.12 第一阶段验收：`signal-core` spec 全部场景映射到核心文档条款并逐条核销；核心文档内部交叉引用无孤儿；`openspec validate` 通过；与用户共同确认

## 2. 插件 SDK 规范（新增，第二阶段）

- [x] 2.1 SDK 规范引用核心事件 Schema v1，定义事件订阅与处理契约、插件对未知版本事件的处理
- [x] 2.2 定义插件注册与生命周期：目录约定、`register(app)` 入口、启动加载顺序、加载失败隔离、无热加载
- [x] 2.3 定义通知 API 与命令路由：消息提交、管理员校验、高风险命令二次确认接入、命令去重
- [x] 2.4 定义存储隔离：插件表前缀命名空间、核心与插件互不读写、插件独立迁移版本管理
- [x] 2.5 定义故障熔断：handler 异常包裹、连续失败阈值、熔断与解除规则

## 3. 交易插件文档（新增，第二阶段）

- [x] 3.1 凭证与环境：DBOTX_API_KEY、分链钱包 ID、模式（PAPER/LIVE）、凭证缺失禁用行为
- [x] 3.2 DBotX 交易契约：`swap_order` 与 `sim_swap_order` 参数映射（chain/pair/walletId/amountOrPercent/费用字段/滑点），服务端 TP/SL 组（stopEarnGroup/stopLossGroup/trailingStopGroup/pnlOrderExpire*）映射与默认值占位
- [x] 3.3 WS 契约：`subscribeTradeResults` 事件类型全集、30s 心跳、断线重连与事件幂等（远端订单 ID 幂等键）
- [x] 3.4 对账契约：`swap_orders` 批量查询、`/kline/wallet/assets`（pnl 字段）、`account/swap_trades`、`account/wallets` 验证；重启恢复流程
- [x] 3.5 风险账本：敞口/日亏损/连续亏损/冷却/最大持仓数，事务内预留，PAPER/LIVE 隔离
- [x] 3.6 Telegram 命令：/mode、/positions、/closeall 的语义与二次确认流程
- [x] 3.7 标记待契约测试项：Simulator list_tasks、kline 字段单位、WS 事件实际字段（以实测锁定契约）
- [x] 3.8 继承现文档 §7.3 ENTRY_GATE 与 §7.4-7.6 状态机语义并大幅简化（删除 UNKNOWN 冻结机与 TAKE_PROFIT_BATCH），改写为插件内开仓前置检查与仓位状态定义

## 4. 波段插件文档（新增，第二阶段）

- [x] 4.1 行情数据源：`/kline/chart` 参数与分页（1s~1d、100 条/页、end 参数）、Pair Updates WS 契约
- [x] 4.2 服务端触发映射：Limit Order `settings[]`（triggerPriceUsd/triggerDirection/useMidPrice/expireExecute）与箱体规则的对应
- [x] 4.3 循环状态机：空仓/持仓、轮次、止盈卖出确认、箱体破坏/安全恶化/流动性崩塌终止条件
- [x] 4.4 资金隔离与记账：波段专属钱包（或显式共享配置）、盈亏记录表设计

## 5. 最终验收

- [x] 5.1 将 `plugin-sdk`/`trade-plugin`/`swing-plugin` 三个规格的全部场景映射到对应文档条款，逐条核销
- [x] 5.2 交叉引用检查：四份文档互引无循环、无孤儿引用、术语一致
- [x] 5.3 运行 `openspec validate`（严格模式）通过
- [x] 5.4 旧文档与重构过程文件处置（管理员终裁）：废弃的旧总控文档与 docs/ 下重构过程文件全部删除，避免 agent 误读；归档记录以本变更目录为准
