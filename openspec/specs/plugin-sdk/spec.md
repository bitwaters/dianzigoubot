# plugin-sdk Specification

## Purpose
定义核心与插件之间的全部接口契约：进程内插件注册与生命周期、事件订阅、通知发送、命令路由与存储隔离，保证插件可以独立开发、独立演进。
## Requirements
### Requirement: 进程内插件注册
插件 SHALL 与核心运行在同一 asyncio 进程中，通过声明式注册机制（插件包暴露注册入口，核心启动时加载）接入。插件 MUST NOT 被热加载；插件的启用/停用 MUST 通过重启核心生效。插件加载失败 MUST 只影响自身，不阻断核心启动。插件 MUST NOT 执行阻塞调用（同步 IO、长时间计算），事件处理必须为非阻塞异步实现。

#### Scenario: 插件加载失败隔离
- **WHEN** 某个插件在注册阶段抛出异常
- **THEN** 核心记录告警并跳过该插件，核心其余功能正常启动

#### Scenario: 阻塞处理函数拒绝注册
- **WHEN** 插件尝试注册同步（非异步）的事件处理函数
- **THEN** 注册被拒绝并记录告警，插件不得以阻塞方式接入事件循环

### Requirement: 事件契约
插件总线 SHALL 提供 `signal_created` 与 `signal_invalidated` 两类业务事件。事件载荷 MUST 使用带版本号的 JSON Schema 校验，包含：`schema_version`、`signal_id`、`chain`、`token_address`、`pool_address`、`signal_level`、`total_score`、`reference_price`、`security_snapshot`、`expires_at`、`strategy_revision`。插件 MUST 对未知版本事件显式拒绝并告警，不得静默吞掉。

#### Scenario: 未知事件版本告警
- **WHEN** 插件收到事件版本高于其支持的 Schema 版本
- **THEN** 插件拒绝处理并产生告警，核心继续运行

### Requirement: 通知 API
核心 SHALL 向插件提供统一通知 API（消息文本 + 可选目标 chat/channel），由核心 outbox 负责投递与幂等。插件 MUST NOT 直接持有 Telegram bot token 或调用 Telegram API。

#### Scenario: 插件通知入 outbox
- **WHEN** 交易插件调用通知 API 发送成交通知
- **THEN** 消息写入核心统一 outbox，使用核心投递机制送达管理员

### Requirement: 命令路由
核心 SHALL 支持插件注册管理员命令（如 `/positions`、`/closeall`）。命令 MUST 只响应配置的管理员 ID；命令处理 MUST 经过核心的统一去重与确认机制（高风险命令沿用一次性确认 nonce）。

#### Scenario: 插件命令路由
- **WHEN** 管理员向机器人发送交易插件注册的 `/positions`
- **THEN** 核心将该命令分发给交易插件处理，非管理员消息被拒绝

### Requirement: 存储隔离
插件 SHALL 使用共享 SQLite 中带插件前缀的命名空间表。核心 MUST NOT 读写插件表；插件 MUST NOT 读写核心业务表（tokens/pools/candidates/signals 等）。插件迁移 MUST 走独立于核心迁移的版本管理。

#### Scenario: 表命名空间隔离
- **WHEN** 交易插件创建订单表
- **THEN** 表名为插件前缀命名（如 `trade_orders`），核心的迁移与清理任务不触碰该表

### Requirement: 插件故障熔断
核心 SHALL 包裹插件的事件处理调用；同一插件连续处理失败达到阈值后 MUST 熔断该插件（停止向其分发事件）并告警管理员，MUST NOT 影响其他插件与核心自身。熔断解除 MUST 通过重启或管理员显式操作。

#### Scenario: 连续失败熔断
- **WHEN** 某插件处理事件连续 N 次抛出未捕获异常
- **THEN** 核心停止向该插件分发事件并告警，其他插件不受影响

