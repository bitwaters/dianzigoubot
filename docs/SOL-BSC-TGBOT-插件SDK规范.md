# SOL/BSC 潜力币信号引擎——插件 SDK 规范

| 项目 | 规范 |
|---|---|
| 文档性质 | 插件与核心之间全部接口契约的唯一规范；插件开发者与本规范对齐 |
| 适用对象 | 交易插件、波段插件及未来全部插件 |
| 上游契约 | 核心文档《SOL-BSC-TGBOT-信号引擎总控开发文档》第 15 章（插件总线与事件 Schema v1） |
| 技术栈 | Python 3.13+、asyncio 单进程、共享 SQLite |

---

## 1. 定位与设计原则

插件是与核心同进程运行的独立功能模块，通过插件总线接入。设计原则：

1. **三接口原则**：插件与核心的全部交互只有事件订阅、通知、命令三条通道，没有其他耦合面。
2. **独立开发**：插件可独立演进，只需兼容事件 Schema 版本与 SDK 版本；插件凭证与配置自持。
3. **简单优先**：无热加载、无插件间通信、无依赖注入框架；启用/停用通过重启生效。
4. **失败隔离**：插件故障不得拖垮核心与其他插件（见第 7 章熔断）。

## 2. 事件契约

### 2.1 事件 Schema 引用

事件载荷 Schema 由核心文档第 15.2 节锁定（Schema v1，`signal_created` / `signal_invalidated`）。本规范不重复定义字段，只约定订阅与处理契约。核心文档 Schema 变更必须提升 `schema_version`，并同步修改本规范。

### 2.2 订阅与处理契约

- 插件通过 `app.events.subscribe(event_type, handler)` 注册处理函数；`event_type` 为 `"signal_created"` 或 `"signal_invalidated"`。
- `handler` 必须为 async 函数，签名为 `async def handler(event: dict) -> None`；核心对同步函数注册直接拒绝并告警。
- 事件按创建顺序分发给各插件；同一插件内按注册顺序执行。
- **投递语义为至少一次**：事件可能因重启补发等原因重复投递，插件 MUST 以 `event_id` 幂等处理（例如以 `event_id` 唯一约束落库，重复事件直接跳过）。
- **分发超时**：核心对每个插件的处理调用设置分发超时（默认 5 秒）；处理超时或抛出未捕获异常均计一次熔断计数，核心继续处理其他插件，不等待慢插件。
- 插件必须校验 `schema_version`：高于自身声明支持范围的版本必须显式拒绝（记录告警后返回），不得静默吞掉。
- 事件载荷只读：插件不得修改事件对象，不得依赖事件之外的内部实现。

### 2.3 版本声明

插件在元数据中声明支持的 SDK 版本与事件 Schema 版本：

```yaml
# plugins/<name>/plugin.yaml
name: trade
version: 0.1.0
sdk_version: "1"
supported_event_schema_versions: ["1"]
```

声明不含 `"1"` 的插件在加载时被拒绝并告警。

## 3. 插件注册与生命周期

### 3.1 目录约定

```text
plugins/
├── <name>/
│   ├── plugin.yaml        # 元数据与版本声明（必填）
│   ├── register.py        # 注册入口（必填）
│   ├── ...                # 插件自有代码
│   └── migrations/        # 插件独立迁移（见第 6 章）
```

### 3.2 注册入口

`register.py` 暴露唯一入口：

```python
async def register(app: PluginContext) -> None:
    ...
```

`PluginContext` 提供以下属性：`app.events`（订阅）、`app.notify`（通知）、`app.commands`（命令）、`app.storage`（插件命名空间存储）、`app.config`（插件自有配置读取）、`app.log`（带插件前缀的日志）。

### 3.3 加载顺序与启停

- 启用列表由核心配置 `config/plugins.yaml` 的 `enabled: [name, ...]` 定义，按列表顺序依次加载。
- 插件注册阶段抛出的异常只导致该插件被跳过（核心告警后继续启动其他插件）；核心启动不受阻断。
- 无热加载：插件的启用/停用/升级必须通过修改启用列表或代码后重启核心生效。
- 重启后重新执行全部插件的 `register`；插件状态由 SQLite 命名空间表恢复。

### 3.4 非阻塞要求

插件事件处理必须为非阻塞异步实现，禁止同步 IO、长时 CPU 计算或自行创建阻塞线程。违反此约定导致核心事件循环卡顿的插件，按第 7 章熔断。

## 4. 通知 API

- `await app.notify.send(text, *, target="admin", priority=0) -> delivery_key`：提交一条通知，返回核心分配的 `delivery_key`。
- `target` 取值：`admin`（管理员会话，默认）、`channel`（信号频道）、`both`。
- `priority` 为整数，只影响核心 outbox 的调度顺序，不改变投递语义。
- 投递、补发、幂等全部由核心 outbox 负责（核心文档第 8.3 节）；插件不得持有 Telegram Bot Token，不得直接调用 Telegram API。
- `await app.notify.status(delivery_key)`：查询投递状态（可选使用）。

## 5. 命令路由

- `app.commands.register(name, handler, *, requires_confirmation=False)`：注册管理员命令。
- 命令名冲突（核心命令或先加载插件已注册）在注册时被拒绝并告警，不采用覆盖。
- 管理员 ID 校验、`update_id` 去重、确认框架全部由核心执行；插件只处理已鉴权、已去重、已完成确认的命令消息。
- `requires_confirmation=True` 的命令自动接入核心一次性 nonce 确认（核心文档第 8.3 节）。
- `handler` 为 async 函数，异常按第 7 章计熔断。

## 6. 存储隔离

- 插件表使用 `<plugin_name>_` 前缀（如 `trade_orders`、`swing_cycles`），全部位于共享 `bot.sqlite`。
- 核心不读写插件表；插件不读写核心业务表（`tokens/pools/candidates/signals` 等）。跨插件互不读写。
- 插件迁移位于 `plugins/<name>/migrations/`，编号递增的 SQL 文件；`register` 阶段由 `app.storage` 按插件自己的版本号执行未应用迁移。
- 核心迁移与清理任务不触碰插件表；插件的清理只作用于自己的表。

## 7. 故障熔断

- 核心包裹每个插件的事件处理与命令处理调用。
- 连续失败计数：同一插件在滑动窗口内事件/命令处理连续抛出未捕获异常或超过分发超时达到阈值（默认 10 次）时熔断该插件。
- 熔断效果：停止向该插件分发事件与命令；插件已有的定时任务停止调度；核心与其他插件不受影响。
- 熔断触发时经核心通知告警管理员。
- 熔断解除：重启核心（第一阶段只支持重启解除；管理员显式恢复作为后续增强）。

## 8. 配置与凭证

- 插件静态元数据在 `plugins/<name>/plugin.yaml`；运行配置与凭证由插件自持（如环境变量、`plugins/<name>/config.yaml`），核心不配置、不读取、不注入。
- 凭证（API Key、钱包 ID 等）不得进入 Git、日志、通知消息或核心表；插件自行实现最小化脱敏。
- 插件凭证缺失时插件必须进入禁用状态并告警，不得影响核心运行（具体行为见各插件文档）。

## 9. 版本与兼容性

- SDK 语义版本与事件 Schema 版本独立演进；核心文档第 15.2 节的 Schema 变更必须提升 `schema_version` 并在本规范记录兼容说明。
- 插件声明支持版本范围；Schema 大版本不兼容时插件必须显式拒绝（见 2.2），不得静默降级。
- 本规范修改流程与核心文档相同：先改规范，再改实现与测试。
