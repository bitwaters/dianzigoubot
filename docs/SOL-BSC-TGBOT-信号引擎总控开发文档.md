# SOL/BSC 潜力币信号引擎——总控开发文档

| 项目 | 规范 |
|---|---|
| 文档性质 | 系统设计、开发、测试和验收的唯一总控规范（仅信号引擎；交易执行见插件文档） |
| 目标网络 | Solana、BSC |
| 市场与安全数据 | CoinGecko Analyst、GoPlus |
| 交易执行 | 不属于本系统；由交易插件/波段插件独立承担（见插件文档与插件 SDK 规范） |
| 交互入口 | Telegram 私有管理员会话、私有信号频道 |
| 技术栈 | Python 单进程异步架构、SQLite、进程内插件总线 |

---

## 文档导读

本系统由四份文档共同定义，按以下顺序阅读：

```
《信号引擎总控开发文档》（本文档）
  │ 第 15 章：插件总线与事件 Schema v1（对插件的唯一契约）
  ▼
《插件 SDK 规范》── 插件开发者必读：如何接入核心
  ▼
《交易插件文档》── 实盘/模拟交易插件（DBotX 直连）
《波段插件文档》── 单币内低买高卖插件（DBotX 行情 + Limit Order）
```

- **维护者**：改核心行为先改本文档；改插件接口先改本文档第 15 章并同步 SDK 规范。
- **插件开发者**：只需读本文档第 1.3 术语、第 15 章与 SDK 规范；核心其余章节与其无关。
- **第一遍通读建议**：第 2 章（目标职责）→ 第 3.3 节（主数据流）→ 第 5 章（安全）→ 第 6 章（策略字段）→ 第 7 章（候选与信号）；第 4 章契约细节按需查阅。

---

## 1. 文档控制规则

### 1.1 单一事实源

- 本文档定义信号引擎的系统边界、数据流、接口契约、状态、存储、回测和验收规则。
- `config/strategy.yaml` 是唯一可编辑的信号策略规则与数值来源。
- API Key、数据库路径、日志级别等运行参数不属于策略，通过环境变量注入。
- Python 代码不得重复定义策略阈值、权重、可信报价地址和信号安全动作。
- SQLite 中只有一个整体 `ACTIVE` 策略快照；历史快照只用于审计和回退。
- 插件事件契约（Schema 版本）由本文档第 15 章定义；插件自身的配置、凭证与内部契约属于插件文档，核心不持有、不校验。
- 接口能力、字段语义或业务规则发生变化时，先修改本文档，再修改 Schema、代码和测试。

### 1.2 规则优先级

系统按以下优先级运行：

```text
信号质量 > 数据即时性 > 信号数量
```

安全硬门禁不能被成交量、涨幅、热门度或评分抵消。资金安全由交易插件在其自身文档中定义，核心无资金职责。

数据源集合（CoinGecko 为主、GoPlus 为安全补充）与评分维度集合在本版本文档冻结：新增数据源或评分维度必须先经回测证明信号质量提升（见第 10 章），经管理员确认后通过正式变更流程进入核心；否则只能进入插件或 backlog。

### 1.3 术语

| 术语 | 固定含义 |
|---|---|
| `W5/W10` | 决策时最新连续 5/10 根 `final=true` 的 G3 1 秒 K 线，窗口内不允许缺秒 |
| `signal_generation` | 同一链、代币和策略修订下，从首次满足可发信号条件到重新武装前的唯一信号世代 |
| 池选择屏障 | 所有已准入池进入终态或到达统一截止时间后，才允许选择唯一决定池的同步点 |
| `delivery_key` | Telegram 业务事件的唯一投递键，同一事件重启、补发和恢复时保持不变 |
| 插件 | 与核心同进程运行、通过插件总线接入的独立功能模块（交易插件、波段插件等） |
| 插件总线 | 核心提供的事件发布、通知、命令路由与插件生命周期管理的统称 |
| 信号事件 | 核心经插件总线发布的 `signal_created` / `signal_invalidated` 事件，Schema 见第 15 章 |

---

## 2. 系统目标与职责

### 2.1 目标

系统持续发现 Solana 和 BSC 上已经形成市场关注度、真实成交或明显异动的代币，完成安全检查、市场质量筛选、可解释评分、防追高判断、信号落库、Telegram 推送，并通过插件总线为插件提供统一信号事件。

热门和持续动量代币是主要对象。新池只作为受限发现标签，必须完成存活、安全、流动性、持仓结构和可卖性验证后才能产生交易信号。

### 2.2 组件职责

| 组件 | 负责 | 不负责 |
|---|---|---|
| CoinGecko REST | 热门、新池、市场快照、池、持仓、成交、历史 OHLCV、额度 | 交易执行 |
| CoinGecko WebSocket | G1 已知候选顶部池价格触发、G3 精确池 1 秒 OHLCV | 全市场发现、G2 逐笔成交订阅和长期归档 |
| CoinGecko 安全字段 | 第一层安全与质量过滤 | 单独证明代币绝对安全 |
| GoPlus | CoinGecko 之后的双链安全补充和信号前复检 | 热门度、动量和价格预测 |
| Telegram | 信号推送、状态查询、管理员控制、插件命令路由 | 私钥保存和交易签名 |
| SQLite | 运行状态、策略快照、有限行情、回测和审计 | 全市场历史行情仓库 |
| 插件总线 | 信号事件发布、通知 API、命令路由、插件加载与熔断 | 交易执行、插件内部状态管理 |

---

## 3. 技术架构

### 3.1 固定技术栈

```text
Python：3.13+
并发：asyncio 单进程
CoinGecko/GoPlus REST：httpx.AsyncClient
WebSocket：websockets
Telegram：python-telegram-bot Long Polling
配置校验：Pydantic
数据库访问：aiosqlite
数据库：SQLite
测试：pytest
部署：单个 Docker 容器
```

应用、迁移、单元测试、集成测试和验收均使用 Python 3.13。临时 API 能力探测脚本不定义应用运行时。自本元数据规则纳入本文档后新生成或重新执行的探测报告，必须记录实际 Python 版本、脚本版本和 UTC 执行时间；此前缺少这些字段的报告在加载或引用时固定归类为 `LEGACY_EVIDENCE`，原文件不补写无法追溯的元数据，只能与官方文档和当前 client 契约测试共同佐证已锁定契约，不能单独支持新增或变更契约。任何旧能力重新探测时必须生成符合当前元数据规则的新报告。

### 3.2 运行任务

一个 Python 进程运行五个异步任务，插件总线与插件在同一事件循环内运行：

1. `discovery_task`：CoinGecko 统一发现、详情补充和 GoPlus 预检。
2. `market_task`：CoinGecko G1/G3 WebSocket、候选 5/10 秒滚动窗口。
3. `strategy_task`：双链筛选、评分、信号判断和结果标签追踪调度。
4. `telegram_task`：推送通知、处理管理员命令和插件命令路由。
5. `storage_task`：SQLite 优先级写入、清理、备份、额度和健康检查。

插件总线（第 15 章）承担插件加载、事件分发、通知提交与插件熔断。

### 3.3 主数据流

```text
CoinGecko 统一发现
→ 候选代币/池去重与三标签
→ CoinGecko 宽口径市场门禁和发现响应安全预筛
→ REST 池与 G1 顶部池合并
→ CoinGecko 代币级详情、持仓和池级安全检查
→ CoinGecko 详细检查通过后执行 GoPlus PRE_MONITOR_CHECK
→ GoPlus 通过后启动精确池 G3，并行执行聪明钱取证
→ 第二份聚合快照和多池选择屏障
→ SOL/BSC 独立策略
→ GoPlus PRE_EXECUTION_CHECK
→ 信号落库（发布 signal_created 事件）
→ Telegram 买入信号推送

已有信号
→ 信号后结果标签追踪（低频 REST 轮询，15m/1h/6h/24h）
```

Telegram 发送失败不阻止信号落库与事件发布；事件载荷携带 `telegram_status`，是否以投递成功作为开仓前提由插件自行决定。

---

## 4. CoinGecko 接口契约

### 4.1 能力与认证

- 系统按 CoinGecko Analyst 能力开发。
- API Key 只从环境变量读取。
- 所有响应在 client 层归一化；业务层不读取供应商原始结构。
- 地址按链规则规范化，金额和比例使用 `Decimal`，时间统一为 UTC 毫秒。
- HTTP 成功但业务数据缺失、类型错误或无法解析时不得视为有效数据。

### 4.2 REST 发现接口

统一采集调度器使用：

- `/onchain/pools/megafilter`
- `/onchain/networks/{network}/trending_pools`
- `/onchain/networks/{network}/new_pools`

每条链只有一个调度器。热门、异动、新池是候选标签，不是三套采集器或三套策略。

Megafilter 服务端只使用稳定采集边界：

- Solana、BSC 均使用 `good_gt_score`。
- BSC 同时使用 `no_honeypot` 和 `include_unknown_honeypot_tokens=false`。
- Solana Megafilter 不使用 `no_honeypot`，该服务端 checks 能力不适用于 Solana；这与 Token Info 返回同名诊断字段是两个独立契约。
- 使用宽口径最低流动性和最低成交活跃度阻止垃圾池消耗额度。
- FDV、池龄、成交量、买卖笔数、持仓、税率、评分和信号阈值在本地执行，不用于缩窄历史候选集合。

每个查询模板必须在 `{chain}.collection.query_templates` 中声明接口、排序、轮询周期和页数。每条链固定使用 `m5_trending` 30 秒、`m5_price_change_percentage_desc` 120 秒、Trending Pools `duration=5m` 120 秒和 New Pools 180 秒四个入口；Megafilter 每页最多 20 个池，所有实际调用由运行时可用性准入统一控制。

调度器为每个模板独立持久化 `next_due_at`，每次只执行已到期模板；不存在以 30 秒统一触发四个模板的全量轮询。`discovery.rest_refresh_seconds` 只控制已进入候选处理流程的详情快照刷新，不触发 Megafilter、Trending Pools 或 New Pools。

`sort` 只在 Megafilter 模板中作为请求参数发送；Trending Pools 和 New Pools 固定使用 `sort=endpoint_default`，该值只表示保留接口原始顺序，不发送给 API。Megafilter client 请求构造器必须从同链 collection 自动附加 `reserve_in_usd_min=reserve_usd_floor`、`tx_count_min=tx_count_floor` 和 `tx_count_duration=tx_count_duration`；模板 `query` 禁止重复声明这三个键，避免出现两份值。Schema 对每个 Megafilter 模板验证注入源字段存在且最终请求只含一份映射参数，并用构造后的最终 query 执行契约测试。三个值同时对所有来源执行本地早期门禁，禁止依赖接口默认 24h 窗口；非 Megafilter 接口不发送这三个参数。

### 4.3 发现标签

候选进入统一候选池后在本地打标签：

- `hot`：来自 `megafilter_hot` 或 Trending Pools，且在该响应去重后的池序列中名次不大于 `max_source_rank`。
- `anomaly`：`(volume_rate_ratio_m5_to_previous_10m >= volume_acceleration_ratio_min 且 buy_sell_tx_ratio_5m >= buy_sell_tx_ratio_min)`，或 `abs(price_change_5m_pct) >= price_change_5m_abs_pct_min`。任一依赖字段缺失时只令对应分支为 false，不能用默认值补齐。
- `new_pool`：来源为 New Pools，或在本次发现响应接收时满足 `0 <= received_at - pool_created_at <= pool_age_seconds_max`；源时间缺失或位于未来时不打该标签。

规则：

- 代币唯一键：`chain + token_address`。
- 池唯一键：`chain + pool_address`。
- 同一代币可以同时拥有多个标签。Token Info、Top Holders、Top Traders 和 GoPlus 代币级安全按 `chain + token_address` 去重；流动性、LP 保护、Pool Trades、G3、微观结构和评分按 `chain + pool_address` 分别计算；最终仍只进行一次同代币信号处理。
- 信号去重键：`chain + token_address + strategy_revision + signal_generation`；决定池地址保存在信号中但不产生第二条同代币信号。
- 主标签按 `hot > anomaly > new_pool` 选择，只用于资源归属和展示。
- 热门与异动占用不少于 90% 的候选深查和实时订阅容量；新池最多占用 10%。
- `signal_generation` 在候选首次进入可发信号状态时创建。`setup_confirmation_seconds` 只控制首次 SETUP 确认；重新武装必须连续低于 `setup_score_min` 达到 `signal_rearm_below_setup_seconds`。进程重启从 SQLite 恢复 generation，持续满足条件不能重复推送。

### 4.4 REST 详情接口

候选通过采集硬边界后的实时深查只调用：

- Token Info
- Multiple Pools
- Tokens Multi
- Top Holders
- Pool Trades
- Top Traders

Pool OHLCV 与 Token OHLCV 只用于结果补全和历史回放，不进入实时候选深查；运行策略不调用 Token Trades。

候选处理按固定依赖图执行：REST 发现池与 G1 解析出的顶部池合并到同一个 `chain + token_address` 候选；完成第一份 5 分钟聚合快照后，先请求可复用的 CoinGecko 代币级安全、持仓资料和池级安全字段。CoinGecko 详细检查通过后调用 GoPlus `PRE_MONITOR_CHECK`；GoPlus 通过后，所有通过宽口径门禁的精确池分别启动 G3，并并发取得 Top Traders 和各池 Pool Trades。进入 FOCUS 后第 10 秒使用 Multiple Pools 批量取得第二份聚合响应；达到池选择屏障后，从全部已准入且证据完整的池中选出唯一决定池，再对该池执行防追高等待。同一实体在 TTL 内复用缓存，只有存在依赖关系的步骤串行。

可决定池只从"一侧为目标代币、另一侧地址存在于该链 `market_quality.trusted_quote_assets[].address`"的池中选择；可信资产可以位于池的 base 或 quote 侧。Solana 的可信资产为 WSOL、USDC、USDT，BSC 为 WBNB、USDT、USDC、FDUSD；DEX 中原生 SOL/BNB 分别以 WSOL/WBNB 参与池交易。REST 发现池和 G1 顶部池使用同一套流动性、5 分钟成交、参与者、安全、评分和入场结构规则，不存在 G1 专用阈值或第二套策略。通过宽口径门禁的每个池分别进入 G3 FOCUS 并计算完整分数。池选择屏障在第二份聚合快照完成且所有已准入池进入 `ELIGIBLE/REJECT/INCOMPLETE` 终态时打开；最迟在首个 G3 FOCUS 开始后 15 秒打开，未完成池固定记为 `INCOMPLETE`，不得参与本轮选择。随后按 `trade_allowed > 总分 > 有效流动性 > 5 分钟成交量 > pool_address` 的固定顺序选择唯一决定池；防追高只对该决定池执行，触发等待时连续执行第二轮 G3，等待失败时本轮不回退到其他池。没有可信对手资产池时仅观察；不同池的成交和 K 线不得拼接。最终信号的池级价格、成交量、成交方向、K 线和流动性必须全部来自同一个决定池。

G1 消息本身不含池地址。首次订阅、顶部池映射 TTL 到期或 G1 触发异常价格时，系统使用 Top Pools by Token Address 端点逐代币解析顶部池（契约测试锁定：Tokens Multi 的 `include=top_pools` 不返回顶部池，不能用于池映射）。顶部池映射键为 `chain + token_address`，池去重键为 `chain + pool_address`：地址与 REST 已发现池相同时合并 `REST/G1_TOP` 来源标签和最新时间，不重复深查、安全检查、评分或订阅 G3；地址不同时作为同一代币下的另一池证据独立计算，最终仍只选一个决定池。

候选主池发生变化时，立即失效该候选已有的池级窗口、LP 匹配、安全结论和未执行信号，退回 `WATCHING` 后按新主池重新补充数据。信号保存发出时的决定池；信号发出后决定池撤池或流动性崩塌时，该信号立即作废并发布 `signal_invalidated`，发现另一个新池不能抵消该作废条件。

- Pool Trades 固定传 `token=<目标代币地址>`，使 `kind`、金额和价格均相对目标代币；Top Holders 固定传 `include_pnl_details=false`，数量使用明确数值：Solana 传 `holders=40`，BSC 传 `holders=50`，以便排除明确地址后重新计算 Top 10。`holders=max` 不进入运行契约，因为供应商将该值解析为 10 条；明确数值 40/50 分别返回对应上限。系统不请求或解析 holder PnL 明细，禁止依赖供应商默认值。
- CoinGecko Top Holders 的 `last_updated_at` 和 Token Info `holders.last_updated` 必须解析为源数据时间，不能用本地请求完成时间替代。超过 `signals.max_participant_age_seconds`、缺失或位于未来的源时间均记为 `STALE/UNKNOWN` 并禁止发信号。
- `holder_count` 和持币地址增长只参与评分，不设置最低地址数硬门禁。
- `top10_holding_pct` 使用所有新鲜且可执行同一排除规则的 CoinGecko Top Holders 与 GoPlus 地址级结果并取最大值；至少一个来源必须有效，两个来源都无法解析时禁止发信号。Token Info holder distribution 只作诊断记录，不参与阈值或评分，因为其聚合值无法重算地址排除集合。
- Multiple Pools 响应包含 `buy_volume_usd`、`sell_volume_usd`、`net_buy_volume_usd`（m5/m15/m30/h1/h6/h24）。契约测试锁定：该组字段仅在请求 `include_volume_breakdown=true` 时返回。当前策略不将这些字段纳入评分特征（新增特征必须经回测证明，见第 1.2 节与第 10 章）；client 归一化层必须保留该组字段供回测与诊断使用。

#### 4.4.1 盈利交易者与本地优质钱包

系统把 Telegram 展示的"聪明钱"拆为两个可审计来源：

- `TOKEN_PROFITABLE`：CoinGecko Top Traders 中该代币已实现盈利为正的钱包，不代表其跨代币长期优秀。
- `LOCAL_SMART`：系统在不同代币的早期成交样本中反复观察到、达到策略最小样本数、成功率和归零/撤池暴露上限的钱包。

实现分期约定：v1 只实现 `TOKEN_PROFITABLE`（Top Traders 现成数据）；`LOCAL_SMART` 的观察、结果标签与信誉重建机制规范保留在本文档，实现列入 v2。v2 启用前，评分维度中 LOCAL_SMART 相关特征固定禁用（权重为 `"0"`）。

候选通过 CoinGecko 详细检查和 GoPlus `PRE_MONITOR_CHECK` 后调用一次 Top Traders，最终 query 固定为 `traders=20&include_address_label=false`，并对每个进入 FOCUS 的精确池调用 Pool Trades。钱包分类、排除集合和评分不得读取或依赖供应商 address label。Top Traders 钱包先排除创建者、owner、developer、对应池、LP、burn/zero、已验证锁仓合约和由系统其他有效证据识别的合约地址，再分别与该池最近 `smart_money.recent_trade_window_seconds` 的 `tx_from_address` 匹配；每个池独立计算两类钱包的买入数、卖出数、买入金额、卖出金额和净买入金额，不能跨池合并后评分。同一地址同时属于 TOKEN_PROFITABLE 与 LOCAL_SMART 时，活跃钱包和资金全部优先归入 LOCAL_SMART，不再进入 TOKEN_PROFITABLE 数值，但保留两个来源标签用于审计。

Pool Trades 返回少于 `trade_response_limit` 条时，最近窗口为 `COMPLETE`；恰好返回上限条数时，只有最旧成交时间严格早于窗口起点才为 `COMPLETE`，最旧成交时间等于或晚于窗口起点均为 `TRUNCATED`。时间比较使用供应商原始精度，不得先截断到秒；无法解析最旧时间时为 `UNKNOWN`。`TRUNCATED`、`UNKNOWN`、Top Traders 缓存缺失或地址无法归属时，聪明钱特征记 0 分且不能生成卖出阻挡结论。

`LOCAL_SMART` 只评价"该钱包是否反复较早买入随后达到策略目标的代币"，不宣称知道钱包完整账户收益。每个钱包—代币观察以 Pool Trades 的真实早期买入价和时间为基准，只读取同一精确池中开盘时间严格晚于买入所在分钟、且收盘时间不晚于 `买入时间 + success_horizon_seconds` 的真实完整 1 分钟结果条，避免把买入前的同分钟价格用于评价。按分钟升序查找首次触发：最高价达到 `success_gain_pct_min` 为成功触发，最低价达到 `failure_drawdown_pct` 为失败触发；同一分钟同时触发时按失败处理。已确认完整的历史范围中，被接口省略的无成交分钟表示没有新成交价，不触发成功或失败，也不构成数据缺口；只有历史范围本身不完整、主池身份缺失或买入价缺失时才为 `DATA_GAP`。观察期完整但两者均未触发时为 `NEUTRAL`。`SUCCESS/FAILURE/NEUTRAL` 进入 observed token 样本数，成功率分母为三者之和，只有 `SUCCESS` 进入分子；归零/撤池暴露率分母同样为这三类，分子为其中发生归零或撤池的不同代币数。`DATA_GAP` 不进入成功率或暴露率分母，只降低覆盖率。归零/撤池沿用回测结果定义。

钱包信誉不是不可逆累计值。系统长期保存与策略无关的观察身份、早期买入事实和已物化结果状态；当前信誉始终按指定策略 hash、指定决策时间，从该时间之前已经结束观察期的事实重建。修改 `success_horizon_seconds`、`success_gain_pct_min` 或 `failure_drawdown_pct` 时，baseline 与 candidate 必须分别从同一批真实分钟结果重算；所需结果数据不足的观察为 `DATA_GAP`，不得沿用另一策略的分类。达到 `local_wallet_min_observed_tokens` 后才参与实时评分。每个候选决策保存当时可见的 reputation 输入集合、计数、覆盖率、策略 hash 和计算时间；回测禁止读取决策之后结束的观察结果。

数据完整时，至少 `min_confirming_wallets` 个 LOCAL_SMART 或 TOKEN_PROFITABLE 钱包同步净买入进入评分；至少该数量的 LOCAL_SMART 钱包同步净卖出，且 60 秒净卖出达到 5 分钟成交量的 `sell_block_net_volume_pct_min` 时阻止本次发信号。`local_smart_net_buy_60s_as_pct_of_volume_5m` 与 `token_profitable_net_buy_60s_as_pct_of_volume_5m` 的分子固定为对应排他分类钱包最近 60 秒净买入 USD，分母固定为同一池第二份快照的 5 分钟成交量 USD。普通盈利钱包净卖出只扣分，不单独形成硬门禁。

#### 4.4.2 信号后结果标签追踪

信号落库后启动低频结果追踪，用于信号质量统计与回测：

- 在信号发出后 15m、1h、6h、24h 四个定点，用 Multiple Pools 查询决定池，记录价格、区间高低点、流动性与池状态到 `outcome_snapshots`。
- 结果追踪 MUST NOT 占用 G1/G3 订阅；每次查询遵循第 4.6 节的 REST 并发上限。
- 定点查询失败时最多补洞重试一次；连续失败记为该标签的数据缺口。
- 区间高低点来自该信号区间内的 1m 行情缓存（market_bars）；无缓存时为 null 并记录区间数据可用性。
- spike 结论已锁定（2026-08-14 实测）：`simple token_price` 只返回价格，不含流动性与池状态，不满足结果标签需求；结果追踪固定使用 Multiple Pools。
- 历史 OHLCV 回补与结果标签的关系见第 10 章。

### 4.5 WebSocket 使用

`market_task` 固定维护两个 WebSocket 连接：Solana 一个、BSC 一个。两个连接独立鉴权、订阅、心跳和重连，不因单链断线重建另一条链。频道职责固定为：

| 频道 | 用途 | 限制 |
|---|---|---|
| G1 OnchainSimpleTokenPrice | 已知候选的顶部池实时增强触发 | G1 不能发现未订阅代币；消息不含池地址，必须先用 Top Pools by Token Address 解析并绑定精确顶部池，绑定后的池进入统一筛选逻辑 |
| G2 OnchainTrade | 供应商能力探测 | 服务端逐笔返回并逐条计费，不在运行策略中订阅 |
| G3 OnchainOHLCV | 精确池秒级确认 | 候选使用目标代币侧 `1s` |

WebSocket client 报文固定使用以下 ActionCable 契约。G1 先订阅频道，再以当前完整目标集合设置代币：

```json
{"command":"subscribe","identifier":"{\"channel\":\"OnchainSimpleTokenPrice\"}"}
{"command":"message","identifier":"{\"channel\":\"OnchainSimpleTokenPrice\"}","data":"{\"network_id:token_addresses\":[\"<network>:<token>\"],\"action\":\"set_tokens\"}"}
```

G3 使用同一报文层级设置精确池、周期和目标币侧：

```json
{"command":"subscribe","identifier":"{\"channel\":\"OnchainOHLCV\"}"}
{"command":"message","identifier":"{\"channel\":\"OnchainOHLCV\"}","data":"{\"network_id:pool_addresses\":[\"<network>:<pool>\"],\"interval\":\"1s\",\"token\":\"base|quote\",\"action\":\"set_pools\"}"}
```

client 只有收到频道 `confirm_subscription` 和每个池的 `onchain_ohlcv:<network>:<pool>:<interval>:<token_side>` 成功确认后才把 G3 订阅记为 ACTIVE。G1 数据事件按 `n=<network>`、`ta=<token_address>` 归一化；未经 `set_tokens` 目标集合匹配的事件丢弃并记录契约异常。JSON 内层 `identifier/data` 都是字符串化 JSON，不能发送为嵌套对象。

订阅规则：

- WATCHING 候选可进入 G1 WATCH 突发订阅；G1 只触发顶部池映射刷新和统一候选重算，未经池地址绑定的 G1 价格不得进入评分。
- REST 池或已绑定的 G1 顶部池通过同一套 5 分钟宽口径初筛后，各自进入该精确池目标代币侧 G3 `1s` FOCUS；池选择屏障打开后统一选择决定池并发出一条同代币信号。
- G1 WATCH 与每个精确池的 G3 FOCUS 每阶段最长 15 秒且最多接收 15 条收费消息；`g1_watch_rounds_max` 与 `g3_focus_rounds_max` 均固定为 2。10 秒垂直上涨或 VWAP 偏离触发等待时，立即为决定池连续执行第二轮 G3，两个阶段之间不清空窗口，累计观察最长 30 秒。
- 每条链 G1、G3 各自的物理上限均固定为 Analyst 单连接、单频道的 100 个订阅。G1 以代币订阅计数；G3 以 `chain + pool_address + token_side` 精确池订阅计数，候选可用量为每频道 100。
- 每一实时阶段的实际准入订阅数由符合条件的订阅数、频道可用订阅数和第 4.6 节的 WS 每日预算共同决定；一个代币的多个精确池分别占用 G3 订阅。
- WS 计费响应按当前供应商契约本地估算计入当日用量；供应商计费契约变化时必须先更新能力验证结果和客户端计费元数据，不能修改策略阈值来伪装适配。
- G2 运行订阅开关固定为 false，Schema 拒绝启用。
- 候选过期、退回观察或被拒绝时取消无用订阅。
- 单链瞬时断线按 `1/2/4/8/15/30` 秒指数退避并加入正负 20% 随机抖动，30 秒封顶且不设置总尝试次数；连接连续稳定 60 秒后重置退避。鉴权失败或报文契约拒绝时停止该链重连并告警，只有 Secret、客户端版本或管理员恢复动作变化后才重新尝试。
- 单链重连后必须等待 welcome 和频道确认，再根据 SQLite 当前状态重建该链完整目标集合；重建期间暂停该链新信号，已收到事件仍按事件键去重。另一条链不得被其退避、重建或失败状态阻塞。
- 达到频道订阅上限时，先淘汰低分且最久未更新的候选。
- 当日 WS 计费估算达到每日预算上限时不接纳新 G1/G3 候选阶段并告警；已运行阶段自然到期，不提前取消。
- 固定发现任务按配置周期运行；候选详情遵循第 4.6 节的 REST 并发上限，达到上限时保留候选优先级等待。

事件规则：

- G1 事件键为 `chain + token_address + event_timestamp`；只有已绑定且映射未过期的顶部池才能生成池证据。G3 K 线键为 `chain + pool_address + token_side + interval + candle_open_timestamp_ms`，重复更新执行 UPSERT，不累加重复 volume。
- G3 是成交驱动事件，订阅成功不代表不活跃池会周期性产生空 K 线。
- G3 没有收盘标志。候选 1 秒 K 线在下一秒事件到达或 `candle_open_timestamp + 2 秒` 后标记 `final=true`；只有最新 10 个 `candle_open_timestamp` 相邻严格相差 1 秒、完整覆盖决策前连续 10 秒，且第二份主池聚合响应与第一份本地接收时间相隔至少 10 秒时，池才可进入 `ELIGIBLE`。缺少任一秒、时间倒退或时间重复均记为 `INCOMPLETE`，不得用更早K线补位。形成决策后的迟到修正可以更新行情存储，但不得改写已保存决策快照。
- WebSocket 响应是否计费及实际用量只按当前供应商契约和 `/key` 对账结果记录；文档和策略不保存固定单条消耗值。

5 分钟交易数、买卖数、买卖人数和资金流只来自发现响应与 Specific/Multiple Pools 的服务端聚合字段；`m5` 对比 `m15-m5` 计算单位时间加速度。相邻 10 秒快照的滚动窗口差值只作增强/衰减方向提示，不伪装成精确 10 秒交易笔数。

候选实时特征的数据边界固定：5 秒特征使用决策时最新 5 根连续有效 final G3 1 秒 K 线，10 秒特征使用最新 10 根；窗口均左闭右闭，以 `candle_open_timestamp` 排序，不能跨缺口取样。5 分钟特征使用同一精确池服务端聚合。Multiple Pools 响应不提供源数据更新时间，系统只保存两次请求的 `requested_at/received_at` 本地单调时间；`observed_liquidity_drop_between_responses_pct` 只能在第二次响应中实际观察到下降时触发阻挡，响应值相同只能记为 `OBSERVATION_LIMITED`，不能证明流动性稳定，也不能作为安全加分。聪明钱 60 秒窗口直接来自请求时已存在的 Pool Trades 历史，不要求候选额外等待 60 秒。

### 4.6 运行时用量控制

用量控制从简：不做逐请求滑动窗口与原子预留账本，用三层粗控保证系统存活。`/key` 在启动时、每 15 分钟、历史回补前和回补后调用；文档和策略文件不记录套餐月额度、固定月度消耗目标或套餐额度推算结果，运行时只使用 `/key` 返回的当前实际字段。

- **REST 粗控**：发现模板固定 `poll_seconds` 间隔本身就是限流；另设全局 REST 并发上限（`collection.rest_concurrency_max`，必填正整数）。达到上限时新请求排队，关键事务（信号落库、安全复检）优先于候选详情与结果追踪。
- **WS 每日预算**：`collection.websocket.daily_credit_budget`（管理员配置的正数）为每日 WS 计费估算上限。收费响应按当前供应商计费契约本地估算计入当日用量，重启后保留当日计数；达到上限时停止接纳新 G1/G3 候选阶段并告警，已有阶段自然到期。不按套餐推导固定值。
- **告警与展示**：`/key` 剩余额度低于管理员配置的告警阈值时通知管理员。`/budget` 只显示 `/key` 当前实际字段、今日 WS 计费估算、REST 请求计数（按接口），不显示写死的套餐额度或预先计算的月度消耗结论。
- **历史回补**：回补计划只展示接口、链、池、时间范围、粒度和请求批次；回补前展示 `/key` 当前余额，由管理员在知情状态下确认后执行（见第 10 章）。
- `/key` 字段缺失、类型错误或互相矛盾时，停止新发现与新订阅并告警；已有候选、结果追踪与信号处理继续。

---

## 5. 安全门禁

本章中"拒绝"统一表示"不允许产生交易信号"，不涉及任何仓位处置。

### 5.1 安全状态

供应商字段归一化为：

| 状态 | 含义 | 信号行为 |
|---|---|---|
| `SAFE` | 明确安全值 | 继续 |
| `RISK` | 明确危险值 | 按字段动作拒绝或仅观察 |
| `UNKNOWN` | 字段适用但缺失、冲突、过期或解析失败 | 禁止发信号 |
| `NOT_APPLICABLE` | 明确不适用于当前链、Token Program 或池类型 | 保存原因后忽略该字段 |
| `STALE` | 安全快照超过 TTL | 重新检查 |

发信号 `PASS` 只在所有适用硬字段均为 `SAFE` 或 `NOT_APPLICABLE` 时成立。任一 `RISK`、`UNKNOWN` 或 `STALE` 都令 `trade_allowed=false`；"仅观察"表示候选可继续监控，但不能发信号。

### 5.2 检查顺序与 TTL

```text
CoinGecko 采集硬检查（Megafilter 服务端 checks：good_gt_score；BSC 另加 no_honeypot）
→ CoinGecko 本地安检（硬门禁：GT Score、mint/freeze 权限、is_honeypot、
  developer/creator 持仓、Top10 集中度；不通过即拒绝，不消耗 GoPlus）
→ GoPlus PRE_MONITOR_CHECK（补充安检：税率、LP 锁仓、owner 风险、黑名单、
  可铸造/可暂停等 CoinGecko 缺失字段）
→ WebSocket 监控
→ GoPlus PRE_EXECUTION_CHECK
→ 信号落库与事件发布
→ Telegram 买入信号
```

- `PRE_MONITOR_CHECK` 成功快照 TTL 为 15 分钟。
- 信号创建使用不超过 120 秒的 `PRE_EXECUTION_CHECK` 成功快照。
- GoPlus 复检失败或超时只阻止本次发信号。
- GoPlus 鉴权为双模式：默认无 Authorization 调用（免费接口，客户端限速 30 次/分钟）；配置 `GOPLUS_APP_KEY`/`GOPLUS_APP_SECRET` 后按官方签名契约（`sha1(app_key + time + app_secret)`）换取 access_token，以 `Authorization: Bearer` 调用，token 到期自动刷新。有鉴权时客户端限速按实测探测值配置（`GOPLUS_RATE_PER_MINUTE`，默认为实测上限的 80%）。两种模式均为全局令牌桶限速，不突发。

### 5.3 BSC CoinGecko 门禁

- `good_gt_score`
- `no_honeypot`
- `include_unknown_honeypot_tokens=false`
- Token Info `is_honeypot=false` 才为 `SAFE`，`true` 为 `RISK`，字符串 `unknown`、缺失或其他类型为 `UNKNOWN`。
- 本地检查 GT Score、流动性、池龄、5 分钟交易结构和 Top 10 集中度；持币地址数只参与评分，买卖与转账税率由 GoPlus 映射执行。
- `on_coingecko` 和 `has_social` 只作展示与诊断，不构成安全门禁或评分。
- `gt_verified` 只参与评分，不构成安全门禁。

### 5.4 BSC GoPlus 映射

路径相对于 `result.<lowercase_token_address>`。除下文明确声明的对象字段外，布尔字段必须是字符串 `"0"/"1"`。

| 内部字段 | GoPlus 原始字段 | 动作 |
|---|---|---|
| `open_source` | `is_open_source` | `0` 拒绝 |
| `in_dex` | `is_in_dex` | `0` 拒绝 |
| `honeypot` | `is_honeypot` | `1` 拒绝 |
| `cannot_buy` | `cannot_buy` | `1` 拒绝 |
| `cannot_sell_all` | `cannot_sell_all` | `1` 拒绝 |
| `sell_blocked` | `is_honeypot`、`sell_tax` | 蜜罐或卖出税 100% 时拒绝 |
| `fake_token` | `fake_token.value` | 数字或字符串 `1` 拒绝 |
| `gas_abuse` | `gas_abuse` | `1` 拒绝 |
| `airdrop_scam` | `is_airdrop_scam` | `1` 拒绝；`0` 安全；缺失为 `SAFE_NO_EVIDENCE` |
| `honeypot_same_creator` | `honeypot_with_same_creator` | `1` 拒绝 |
| `owner_balance_mutable` | `owner_change_balance` | `1` 拒绝 |
| `ownership_risk` | `hidden_owner`、`can_take_back_ownership` | 任一为 `1` 拒绝 |
| `code_risk` | `selfdestruct`、`external_call` | 任一为 `1` 拒绝 |
| `slippage_risk` | `slippage_modifiable`、`personal_slippage_modifiable` | 任一为 `1` 拒绝 |
| `configurable_risk` | `is_proxy`、`is_mintable`、`transfer_pausable`、`is_blacklisted`、`is_whitelisted`、`anti_whale_modifiable`、`trading_cooldown` | 按 BSC 策略动作拒绝或仅观察 |
| `tax_pct` | `buy_tax`、`sell_tax`、`transfer_tax` | 乘 100 转为 pct，超过阈值拒绝 |
| `creator_owner_pct` | `creator_percent`、`owner_percent` | 乘 100 转为 pct，超过阈值拒绝 |
| `top10_holding_pct` | `holders[].percent` | 地址去重、降序取前十并求和，超过阈值拒绝 |
| `lp_locked_pct` | LP holder 锁定信息 | 仅主池匹配的 V2 池适用；不足拒绝，缺失 UNKNOWN |

字段规则：

- GoPlus EVM 响应没有 `cannot_sell` 和 `malicious_creator` 原始字段，Schema 和代码不得创建这两个必填字段。
- `is_open_source=0` 时，依赖源码的缺失子字段使用 `status=NOT_APPLICABLE, reason_code=PARENT_RISK_OPEN_SOURCE`，最终仍由未开源拒绝。
- `is_in_dex=0` 时，交易子字段缺失使用 `status=NOT_APPLICABLE, reason_code=PARENT_RISK_NOT_IN_DEX`，最终仍由未进入 DEX 拒绝。
- `fake_token` 必须解析对象 `{value, true_token_address}`；仅使用 `value` 判定，兼容数字和字符串 `0/1`。字段整体缺失时记 `SAFE` 并保存 `SAFE_NO_EVIDENCE`；字段存在但对象结构或 `value` 无法解析时记 `UNKNOWN`。
- `gas_abuse` 在官方允许省略时可记 `SAFE`，同时保存 `SAFE_NO_EVIDENCE` 原因。
- `is_airdrop_scam` 和 `other_potential_risks` 缺失均记 `SAFE` 并保存 `SAFE_NO_EVIDENCE`；前者只有值 `1` 拒绝，后者非空时按策略动作处理。字段存在但类型无法解析仍为 `UNKNOWN`。

### 5.5 Solana CoinGecko 门禁

- GT Score 及其 pool、transaction、creation、info、holders 子项；`gt_verified` 只参与评分。
- Token Info `is_honeypot` 在 Solana 契约中为字符串 `"unknown"`，不具备 BSC 布尔门禁语义。值为 `"unknown"` 或字段缺失时固定归一化为 `NOT_APPLICABLE` 并保存 `SOLANA_HONEYPOT_CHECK_UNSUPPORTED`；出现布尔值或其他字符串时视为供应商契约变化，记为 `UNKNOWN`、禁止新信号并告警，不能自动套用 BSC 规则。
- `mint_authority`、`freeze_authority`。
- developer holding、Top 10、主池流动性、池龄和 5 分钟买卖结构；持币地址数只参与评分。
- mint 或 freeze authority 仍存在时直接拒绝。

### 5.6 Solana GoPlus 映射

路径相对于 `result.<token_address>`。权限对象使用 `{status, authority[]}`。

| 内部字段 | GoPlus 原始字段 | 动作 |
|---|---|---|
| `mintable`、`freezable` | `mintable.status`、`freezable.status` | 启用时拒绝 |
| `closable` | `closable.status` | 按策略动作 |
| `metadata_mutable` | `metadata_mutable.status/authority[]` | 按策略动作；恶意 authority 拒绝 |
| `balance_mutable` | `balance_mutable_authority.status/authority[]` | 按策略动作；恶意 authority 拒绝 |
| `default_account_frozen` | `default_account_state` | 值为 `"2"` 时拒绝 |
| `default_state_upgradable` | `default_account_state_upgradable.status/authority[]` | 按策略动作；恶意 authority 拒绝 |
| `non_transferable` | `non_transferable` | `1` 拒绝 |
| `transfer_fee_bps` | `transfer_fee` | 当前和计划费率取最大值，超过阈值拒绝 |
| `transfer_fee_upgradable` | `transfer_fee_upgradable.status/authority[]` | 按策略动作；恶意 authority 拒绝 |
| `transfer_hook_present` | `transfer_hook[]` | 非空按策略动作 |
| `malicious_transfer_hook` | `transfer_hook[]` | 明确恶意时拒绝 |
| `transfer_hook_upgradable` | `transfer_hook_upgradable.status/authority[]` | 按策略动作；恶意 authority 拒绝 |
| `top10_holding_pct` | `holders[].account`、`holders[].percent` | 按 owner account 汇总其多个 token account 的比例，汇总后降序取前十并求和，超过阈值拒绝 |
| `liquidity_lock_risk` | `dex[]`、`lp_holders[]` | 只使用与 CoinGecko 主池精确匹配的数据；无法匹配为 UNKNOWN |

普通 SPL Token 不适用的 Token-2022 字段使用 `status=NOT_APPLICABLE, reason_code=TOKEN_PROGRAM_NOT_2022`。确认属于 Token-2022 后，适用字段缺失或状态无法解析时为 `UNKNOWN`。

CoinGecko Top Holders 与 GoPlus 的双链地址级 Top 10 统计只排除明确的 burn/zero 地址、与 CoinGecko 精确匹配的主池地址，以及已验证锁仓合约地址；普通地址、普通合约和交易所地址不因标签自动排除。Token Info 聚合比例无法重算排除集合，只保存为诊断字段。

可发信号的池保护模型由 `security.allowed_pool_models` 固定：Solana 只接受 `LP_TOKEN_OR_BURN_VERIFIABLE`，要求 CoinGecko 主池与 GoPlus DEX/LP 数据精确匹配且能计算保护比例；BSC 只接受 `V2_LP_TOKEN`，要求主池 LP token 锁仓可精确验证。其他池模型保留观察数据但一律 `trade_allowed=false`。可发信号池必须产生数值型 `lp_locked_pct`；`NOT_APPLICABLE` 不能代替该评分值。

---

## 6. 策略总控文件

### 6.1 文件与激活规则

`config/strategy.yaml` 是唯一可编辑策略文件，完整基准文件随本文档一同交付。运行程序只加载 SQLite 中最后成功激活的快照，编辑文件不会热加载。

顶层固定包含：

```yaml
schema: 1
revision: strategy-<number>
parent_revision: strategy-<number> | null
change_reason: <text>
solana: <complete chain strategy>
bsc: <complete chain strategy>
```

- Solana 与 BSC 必须完整分区，不使用 `common`、YAML 锚点、继承或代码默认值共享数值。
- Schema 必须覆盖配套 `strategy.yaml` 的全部键；代码不得为缺失策略字段提供隐式默认值。
- 金额、百分比、比例和权重写为带引号的十进制字符串并解析为 `Decimal`。
- `pct` 单位为 0–100，`ratio` 为 0–1，`bps` 为万分之一。
- 未知键、缺失必填字段、范围错误和权重不合格时拒绝激活。
- 本文所有 canonical JSON 固定使用 UTF-8、对象键字典序、数组原顺序、无多余空白；`Decimal` 输出为无指数、去除无意义尾零的十进制字符串，布尔值和 null 使用 JSON 原生表示。策略哈希、数据集哈希和 run_id 共用同一实现与测试向量。
- 激活与回退流程见第 10 章；策略中不含任何交易执行、仓位、风险或晋升字段，这些属于交易插件配置。

### 6.2 每条链的固定结构

```text
collection
discovery_channels
discovery
market_quality
security
scoring
smart_money
anti_chase
signals
backtest_model
```

### 6.3 采集与发现字段

| 路径 | 约束 |
|---|---|
| `collection.version` | 非空；普通激活中任何 `collection` 内容变化时必须同步修改且新值从未使用；回退复用历史值 |
| `collection.query_templates[]` | `id/endpoint/sort/poll_seconds/pages/query` 必填；每模板独立维护 `next_due_at` |
| `collection.reserve_usd_floor` | `>=0` USD，宽口径采集下限 |
| `collection.tx_count_floor` | `>=0`，宽口径采集下限 |
| `collection.tx_count_duration` | 固定为 `5m`，同时用于 Megafilter 和本地早期门禁 |
| Megafilter 最终 query | client 自动注入 `reserve_in_usd_min/tx_count_min/tx_count_duration`；模板重复声明任一映射键时拒绝激活 |
| `collection.rest_concurrency_max` | `>0`，全局 REST 并发上限 |
| `collection.websocket` | G1/G3 各 100 最大订阅数、顶部池映射 TTL、WATCH/FOCUS 时长、消息数与最大轮数、G2 禁用开关、`daily_credit_budget`（每日 WS 计费预算，正数必填）；不包含套餐月额度或按套餐推导的补充速率 |
| `discovery_channels.hot` | `enabled/max_source_rank` |
| `discovery_channels.anomaly` | `enabled/volume_acceleration_ratio_min/buy_sell_tx_ratio_min/price_change_5m_abs_pct_min` |
| `discovery_channels.new_pool` | `enabled/pool_age_seconds_max` |
| `discovery.candidate_ttl_seconds` | `>0` |
| `discovery.rest_refresh_seconds` | 固定为 `30`，只控制活跃候选详情快照刷新，不驱动发现模板 |
| `discovery.new_pool_min_age_seconds` | `>=0` |

### 6.4 市场质量字段

每条链分别定义：

- `trusted_quote_assets[]`：`symbol/address` 必填，链内规范化后地址无重复，不要求该资产固定处于 pool quote 侧；BSC 以小写比较，Solana 保持大小写
- `reserve_usd_min`
- `fdv_usd_max/fdv_liquidity_ratio_max`
- `pool_age_seconds_min/max`
- `volume_5m_usd_min`
- `tx_count_5m_min/buys_5m_min/sells_5m_min`
- `independent_buyers_5m_min/independent_sellers_5m_min`
- `buy_sell_tx_ratio_5m_min`

FDV 不设最低值，24 小时成交量不设硬门禁；1 小时及更长窗口只能作展示和结果分析，不参与信号门禁。

### 6.5 评分模型

每条链只有一套评分模型，发现标签不改变评分公式。

固定维度：

```text
market_quality
short_momentum
activity_acceleration
trade_flow
participant_structure
holding_distribution
smart_money_flow
microstructure
```

每个维度包含 `weight` 和 `features`。启用维度权重非负且之和等于 1；启用维度内特征权重非负且之和等于 1。`weight:"0"` 表示禁用维度，其特征不参与权重求和。

每个启用特征只配置：

```yaml
bad: "..."
good: "..."
weight: "..."
missing_action: REJECT | CAP_SETUP | ZERO_SCORE
```

缺失动作的含义固定：

- `REJECT`：候选缺少基础必需市场字段，立即拒绝本轮候选。
- `CAP_SETUP`：该特征计 0 分，候选等级最高为 `SETUP`，补齐并刷新数据后可重新评分。
- `ZERO_SCORE`：该特征计 0 分，不额外限制信号等级。

特征方向、窗口和单位固定：

| 维度 | 特征 |
|---|---|
| `market_quality` | `reserve_usd`、`volume_liquidity_ratio_5m`、`fdv_liquidity_ratio`、`gt_score`、`gt_verified` |
| `short_momentum` | `price_change_5s_pct`、`price_change_10s_pct`、`price_change_5m_pct` |
| `activity_acceleration` | `tx_count_5m`、`volume_rate_ratio_m5_to_previous_10m`、`tx_rate_ratio_m5_to_previous_10m` |
| `trade_flow` | `buy_sell_tx_ratio_5m`、`net_buy_tx_pct_5m` |
| `participant_structure` | `independent_buyers_5m`、`buyer_seller_ratio_5m`、`holder_count` |
| `holding_distribution` | `top10_holding_pct`、`developer_or_creator_holding_pct`、`lp_locked_pct` |
| `smart_money_flow` | `local_smart_net_buy_60s_as_pct_of_volume_5m`、`token_profitable_net_buy_60s_as_pct_of_volume_5m`、`active_smart_wallets_60s` |
| `microstructure` | `close_location_10s`、`positive_1s_bar_ratio_10s`、`volume_retention_ratio_10s` |

反向特征只有：

- `fdv_liquidity_ratio`
- `top10_holding_pct`
- `developer_or_creator_holding_pct`

其余为正向特征。

Schema 对 `bad`、`good` 和运行时特征值使用相同的逐特征值域，不能设置全局 `bad >= 0`：

| 值域 | 特征 |
|---|---|
| `>=0` | `reserve_usd`、两个流动性比率、`tx_count_5m`、两个加速度比率、两个买卖/参与者比率、`independent_buyers_5m`、`holder_count`、`active_smart_wallets_60s`、`volume_retention_ratio_10s` |
| `0–100` | `gt_score`、`top10_holding_pct`、`developer_or_creator_holding_pct`、`lp_locked_pct` |
| `0–1` | `gt_verified`、`close_location_10s`、`positive_1s_bar_ratio_10s` |
| `-100–100` | `net_buy_tx_pct_5m` |
| `>=-100` 的有限 Decimal | 三个价格涨跌特征 |
| 有限有符号 Decimal | 两个聪明钱净买入占比 |

计数类和非负比率的 `bad` 可以等于 `0`；有符号特征允许负阈值。NaN、Infinity 和超出对应值域的配置或运行值一律拒绝。

所有派生特征使用以下唯一公式。`W5` 和 `W10` 分别表示最新连续 5/10 根 final G3 1 秒 K 线；`o_i/h_i/l_i/c_i/v_i` 是按时间升序的开高低收和成交量，`H=max(h_i)`、`L=min(l_i)`。价格、金额、比例和分数全程使用 `Decimal`，中间步骤不舍入；持久化特征和分数统一保留小数点后 8 位并使用 `ROUND_HALF_EVEN`。公式要求为正的分母小于或等于 0、源值非法或窗口不完整时，该特征执行自身 `missing_action`。

| 特征 | 唯一计算规则 |
|---|---|
| `reserve_usd` | 决定池第二份 Multiple Pools 响应的 `reserve_in_usd` |
| `volume_liquidity_ratio_5m` | `volume_5m_usd / reserve_usd` |
| `fdv_liquidity_ratio` | `fdv_usd / reserve_usd` |
| `gt_score/gt_verified` | 决策时有效 Token Info 规范化值；布尔值分别映射为 `1/0` |
| `price_change_5s_pct` | `(W5[-1].c / W5[0].o - 1) × 100` |
| `price_change_10s_pct` | `(W10[-1].c / W10[0].o - 1) × 100` |
| `price_change_5m_pct` | 决定池第二份聚合响应的服务端 `m5` 涨跌百分比 |
| `tx_count_5m` | 服务端 `transactions.m5.buys + transactions.m5.sells` |
| `volume_rate_ratio_m5_to_previous_10m` | `(volume_m5 / 5) / ((volume_m15 - volume_m5) / 10)` |
| `tx_rate_ratio_m5_to_previous_10m` | `(tx_m5 / 5) / ((tx_m15 - tx_m5) / 10)`，其中每个窗口的 `tx=buys+sells` |
| `buy_sell_tx_ratio_5m` | `buys_m5 / max(sells_m5, 1)`；使尚无卖单但已有买单的早期池仍可被发现，正式市场门禁仍要求最低卖单数 |
| `net_buy_tx_pct_5m` | `(buys_m5 - sells_m5) / (buys_m5 + sells_m5) × 100` |
| `independent_buyers_5m` | 服务端 `transactions.m5.buyers` |
| `buyer_seller_ratio_5m` | `buyers_m5 / max(sellers_m5, 1)`；正式市场门禁仍要求最低独立卖家数 |
| `holder_count` | 决策时未过期的 Token Info holder count |
| `top10_holding_pct` | 第 4.4、5.4、5.6 节规定的排除和双源取最大规则 |
| `developer_or_creator_holding_pct` | Solana 使用 developer holding，BSC 使用 `max(creator_percent, owner_percent) × 100` |
| `lp_locked_pct` | 与决定池精确匹配且通过允许池模型验证后的数值 |
| `local_smart_net_buy_60s_as_pct_of_volume_5m` | LOCAL_SMART 排他分类钱包 60 秒 `(买入USD-卖出USD)` 之和除以决定池 `volume_5m_usd`，再乘 100 |
| `token_profitable_net_buy_60s_as_pct_of_volume_5m` | TOKEN_PROFITABLE 排他分类钱包按相同公式计算 |
| `active_smart_wallets_60s` | 两个排他分类中 60 秒净买入 USD 大于 0 的不同钱包数 |
| `close_location_10s` | `(W10[-1].c - L) / (H - L)`；`H=L` 时固定为 `0.5` |
| `positive_1s_bar_ratio_10s` | `W10` 中 `c_i > o_i` 的 K 线数量除以 10；平盘不计正向 |
| `volume_retention_ratio_10s` | `Σ(v_6..v_10) / Σ(v_1..v_5)` |

CoinGecko Multiple Pools 响应现已提供 m5~h24 的 `buy_volume_usd/sell_volume_usd/net_buy_volume_usd`；当前策略不将其纳入特征（新增特征必须经回测证明，见第 10 章），client 归一化层保留这些字段供回测与诊断。普通 `trade_flow` 不从 Pool Trades 逐笔重建 5 分钟金额，也不创建 `buy_sell_volume_ratio_5m` 或 `net_buy_volume_pct_5m`。Pool Trades 只为聪明钱的短窗口取证使用；其截断不会污染服务端 5 分钟笔数特征。聪明钱数据为 `TRUNCATED/UNKNOWN` 时三个聪明钱特征均为 0，不能由缺失数据推导没有卖压。

Schema 强制正向特征 `good > bad`，反向特征 `good < bad`，禁止相等；评分函数不得接受会造成除零或方向反转的配置。

```text
正向分数 = clamp((value - bad) / (good - bad), 0, 1) × 100
反向分数 = clamp((bad - value) / (bad - good), 0, 1) × 100
维度分数 = Σ(特征分数 × 特征权重)
总分 = Σ(维度分数 × 维度权重)
```

计算顺序：

```text
安全硬门禁
→ 市场字段归一化
→ 特征分数
→ 维度分数
→ 总分
→ 聪明钱完整性与卖出阻挡
→ 当前入场结构
→ 信号等级
```

### 6.6 信号与防追高字段

每条链分别定义：

- `scoring.watch_score_min/setup_score_min/buy_score_min`
- `signals.validity_seconds/setup_confirmation_seconds/signal_rearm_below_setup_seconds/min_g3_final_1s_bars`
- `signals.g3_bar_final_delay_seconds/max_g3_age_seconds`
- `signals.max_rest_market_age_seconds/max_participant_age_seconds`
- `anti_chase.vertical_rise_10s_wait_pct/vwap_10s_deviation_wait_pct/max_wait_seconds`
- `anti_chase.drawdown_from_10s_high_block_pct/observed_liquidity_drop_between_responses_block_pct`
- `anti_chase.consolidation_seconds_min/consolidation_range_pct_max`
- `anti_chase.pullback_min_pct/pullback_max_pct/pullback_recovery_ratio_min`

历史 5 分钟涨幅不设上限。防追高只使用决定池，并按以下确定性状态机执行：

1. 初始连续 `W10` 的 `vertical_rise_pct = max(0, (最后收盘价 / 第一根开盘价 - 1) × 100)`。每根 K 线的典型价为 `(h+l+c)/3`，`vwap_10s = Σ(典型价×成交量)/Σ成交量`，`vwap_deviation_pct = max(0, (最后收盘价/vwap_10s-1)×100)`；总成交量为 0 时 VWAP 缺失并禁止进入 BUY。
2. 两者均不超过等待阈值时不启动第二轮；任一超过时，以初始窗口最后一根的结束时间为 `wait_started_at`，保持同一 G3 序列连续观察，直到确认入场结构或 `max_wait_seconds` 到期。第二轮不能重置高点、低点或窗口。
3. 等待期间 `H` 为初始 `W10` 至当前的最高价，`L_after` 为等待开始后至当前的最低价，`drawdown_from_10s_high_pct=max(0,(H-current_close)/H×100)`。该值大于 `drawdown_from_10s_high_block_pct` 时立即失败。
4. **高位整理**：等待开始后至少存在 `consolidation_seconds_min` 根连续 final 1 秒 K 线；取最新这么多根，`range_pct=(max(high)-min(low))/min(low)×100` 不超过 `consolidation_range_pct_max`，且当前价格相对 `H` 的回撤严格小于 `pullback_min_pct`。
5. **回踩恢复**：`pullback_depth_pct=(H-L_after)/H×100` 位于闭区间 `[pullback_min_pct,pullback_max_pct]`，并且 `recovery_ratio=(current_close-L_after)/(H-L_after)` 不小于 `pullback_recovery_ratio_min`。分母为 0 时不成立。
6. 高位整理或回踩恢复任一成立即可结束等待；到期仍不成立则本轮拒绝。所有比较均在新 final K 线到达后执行，阈值"超过"使用 `>`，"达到/不低于/不超过"分别使用 `>=/>=/<=`。

两次 Multiple Pools 响应的 `observed_liquidity_drop_between_responses_pct=max(0,(reserve_first-reserve_second)/reserve_first×100)`；第一份储备不大于 0 或任一响应缺失时池为 `INCOMPLETE`，实际下降大于对应阻挡阈值时拒绝。相同响应值产生 `OBSERVATION_LIMITED`，不加分也不单独拒绝。开仓执行价格偏差与滑点上限属于交易插件配置，不在核心策略中定义。

### 6.7 聪明钱字段

每条链分别定义：

- `top_traders_limit/recent_trade_window_seconds/trade_response_limit`
- `local_wallet_min_observed_tokens/local_wallet_success_rate_min_pct/local_wallet_rug_rate_max_pct`
- `success_horizon_seconds/success_gain_pct_min/failure_drawdown_pct`
- `min_confirming_wallets/sell_block_net_volume_pct_min`

`top_traders_limit <= 50`，运行策略固定为 20；`trade_response_limit` 固定为 CoinGecko 上限 300。钱包统计必须按链隔离，同一字符串地址不能跨链合并。

### 6.8 安全字段

Solana 必须配置：

- CoinGecko GT Score 最低值固定为 `75`
- Top 10、developer holding 上限；holder count 不得作为硬门禁
- GoPlus transfer fee 上限、LP locked 下限
- `allowed_pool_models` 固定包含 `LP_TOKEN_OR_BURN_VERIFIABLE`
- closable、metadata mutable、transfer fee upgradable、default state upgradable、balance mutable、transfer hook、Token-2022 风险动作

BSC 必须配置：

- CoinGecko GT Score 最低值固定为 `75`
- Top 10 上限；holder count 不得作为硬门禁
- buy/sell/transfer tax 上限
- creator/owner holding 上限、LP locked 下限
- `allowed_pool_models` 固定包含 `V2_LP_TOKEN`
- proxy、mintable、pause、blacklist、whitelist、anti-whale、cooldown、other risks 动作

固定拒绝字段不允许在策略中改写。

### 6.9 回测成交模型字段

每条链分别定义（仅供回测引擎的 1m 成交模型使用，见第 10 章）：

- `backtest_model.base_slippage_bps/max_impact_bps/impact_coefficient`
- `backtest_model.network_fee_native/min_fill_liquidity_usd`
- `backtest_model.fill_deviation_pct_max/fill_deadline_seconds`，其中 `fill_deadline_seconds >= 180`
- `backtest_model.take_profit_legs[]`：`leg_index/trigger_profit_pct/sell_pct_of_initial`，按 `leg_index` 与触发涨幅严格递增，`sell_pct_of_initial` 总和不得超过 100
- `backtest_model.stop_loss_pct`
- `backtest_model.trailing_stop_pct/trailing_activation_profit_pct`
- `backtest_model.max_hold_seconds`

实盘与模拟执行的滑点、费用、止盈止损与仓位风险参数由交易插件自持，见交易插件文档；核心策略不包含这些字段，回测与实盘参数独立配置、互不依赖。

---

## 7. 候选与信号

### 7.1 候选状态

```text
WATCHING / SETUP / SIGNAL / REJECTED / EXPIRED
```

- 安全未知、仅观察、补充失败等细节写入 `status_reason` 和 `trade_allowed`，不增加状态枚举。
- 候选可以在 `WATCHING` 与 `SETUP` 间变化。
- 新池在完成最低存活时间和全部门禁前保持 `trade_allowed=false`。
- 完成基础特征后，总分低于 `setup_score_min` 为 `WATCHING`；达到 `setup_score_min` 时进入 `SETUP` 并记录 `setup_started_at`，中途低于该值立即清空计时。总分达到 `buy_score_min`、连续 SETUP 时间达到 `setup_confirmation_seconds`、决定池入场结构成立且全部硬门禁通过时才进入 `SIGNAL`。`watch_score_min` 只用于 WATCHING 候选的 G1/G3 资源优先级，不改变硬门禁或信号阈值。

### 7.2 信号记录

信号保存：

- `telegram_status=PENDING/SENT/FAILED/DELIVERY_UNKNOWN`
- 触发原因、评分明细、风险、失效条件、安全快照时间、`strategy_revision` 和 `strategy_hash`

策略产生买入条件后，先完成 GoPlus `PRE_EXECUTION_CHECK`（120 秒快照，见 5.2），再创建信号记录、发布 `signal_created` 事件并发送 Telegram；Telegram 推送不得早于最终安全复检。同一 `chain + token_address + strategy_revision + signal_generation` 只产生一条信号，持续满足条件不能重复推送（见 4.3）。

信号生命周期与事件：

- 信号创建时发布 `signal_created`，载荷 Schema 见第 15 章。
- 信号过期、决定池撤池或流动性崩塌（见 4.4）、或管理员显式作废时，发布 `signal_invalidated`。
- 信号有效期从信号创建时刻起算，由 `signals.validity_seconds` 定义；过期不影响已发布的 `signal_created` 历史事件，插件是否因过期放弃开仓由插件自行决定（事件载荷携带 `expires_at`）。
- `telegram_status` 的演进遵循第 8 章投递幂等规则；投递失败不撤回事件（见 3.3）。

---

## 8. Telegram 控制

### 8.1 信号内容

每条买入信号包含：

- 链、名称、符号、合约地址、主池和 DEX
- 信号等级、总分、评分维度和策略 revision/hash
- 触发价、当前价、有效期
- 流动性、成交量、池龄和买卖结构
- Top 10、developer/creator holding
- 聪明钱方向、LOCAL_SMART/TOKEN_PROFITABLE 活跃钱包数、最近 60 秒净买入金额和数据完整度
- CoinGecko 与 GoPlus 安全结论和时间
- 触发原因、风险因素和失效条件

### 8.2 管理命令

核心命令：

- `/status`
- `/sol`、`/bsc`
- `/watchlist`
- `/signals`
- `/budget`
- `/strategy status`
- `/strategy backtest`
- `/strategy validate-collection`
- `/strategy activate`
- `/strategy rollback`
- `/telegram retry <delivery_key>`
- `/pause`
- `/resume`

`/pause` 只暂停新信号生成。管理命令只接受配置的管理员 ID。

命令路由：

- 插件通过插件总线注册自己的命令（如交易插件的 `/positions`、`/closeall`、`/mode`）；核心负责管理员校验、命令去重与确认框架，参数原样分发给注册插件。
- 插件命令与核心命令冲突时，核心拒绝注册并告警。
- 交易相关命令（/positions、/orders unknown、/wallet orphan、/wallet recheck、/mode、/paper reconcile、/order resolve、/closeall、/risk）不属于核心，见交易插件文档。

### 8.3 确认、Long Polling 与投递幂等

- `/strategy activate`、`/strategy rollback`、`/telegram retry` 必须二次确认。机器人先发送命令摘要、影响范围和 60 秒倒计时，并生成 128-bit 随机 nonce；Inline Keyboard callback data 固定为 `confirm:<base64url_nonce>` 或 `cancel:<base64url_nonce>`。插件声明的高风险命令复用同一确认框架。
- nonce 持久化并绑定管理员 ID、chat ID、原始 Telegram `update_id`、完整命令参数哈希和过期时间，只能使用一次。重复 callback、参数变化、非管理员、跨 chat 或超时全部拒绝；确认成功与业务状态变更在同一 SQLite 事务中消费 nonce。
- Long Polling 的每个 `update_id` 在 `telegram_updates` 中唯一。业务事务提交后才把持久化 offset 推进到 `update_id+1`；掉线或重启从该 offset 继续，重复 update 只返回已保存结果，不重复执行命令。
- 所有外发消息先进入 `telegram_outbox`，状态固定为 `PENDING/SENDING/SENT/FAILED/DELIVERY_UNKNOWN`，以 `delivery_key` 唯一约束业务事件。明确发送成功后保存 Telegram `message_id` 并标记 `SENT`；明确的可重试失败保持同一 outbox 行和同一 `delivery_key` 退避补发，不能创建第二个业务事件。
- Telegram 恢复或进程重启后自动重新调度从未发送的 `PENDING`，沿用原 `delivery_key`；恢复时遗留的 `SENDING` 因无法证明是否已经发送，必须先转为 `DELIVERY_UNKNOWN`，不得自动补发。买入信号补发始终保留原 `expires_at`，不能延长或重新计时；补发时已经过期的消息必须标记"已过期，仅通知"。插件是否放弃开仓由插件依据事件载荷中的 `expires_at` 与 `telegram_status` 自行决定。
- 发送超时或连接中断导致结果不确定时标记 `DELIVERY_UNKNOWN`，不得自动补发买入信号。管理员核对后可使用 `/telegram retry <delivery_key>` 二次确认重新发送；消息正文必须带同一短 delivery key，便于识别可能重复。

---

## 9. SQLite 与数据保留

### 9.1 数据库设置

只使用一个 `bot.sqlite`：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = FULL;
```

### 9.2 写入规则

- 使用一个 `asyncio.PriorityQueue(maxsize=5000)`。
- 优先级固定为：策略事务 > 信号与安全快照 > 候选和候选行情。
- 1m 行情在内存按实体和分钟合并后入队。
- 队列达到 80% 时暂停发现、新候选订阅和新信号生成，恢复到 50% 以下后继续；队列满时只允许丢弃尚未形成信号的候选行情，信号落库或关键事务无法入队必须立即进入故障状态并告警。
- 插件使用带前缀命名空间的独立表，插件写入走各自的通道，不进入核心优先级队列；命名空间规则见第 15 章。

### 9.3 核心表

| 表 | 用途 |
|---|---|
| `tokens`、`pools` | 代币、池和主池关系 |
| `candidates` | 候选状态、标签、过期时间和拒绝原因 |
| `market_bars` | 有限 Pool/Token 1m/5m/15m 行情及币种方向 |
| `security_checks` | 安全快照和 TTL |
| `signals` | 信号、评分、事件与 Telegram 状态 |
| `telegram_updates` | Long Polling update 去重、持久化 offset 和命令处理结果 |
| `telegram_outbox` | delivery_key、消息状态、message_id、补发和结果不确定状态 |
| `telegram_confirmations` | 一次性确认 nonce、绑定参数、过期和消费状态 |
| `strategy_snapshots` | ACTIVE 和历史策略快照 |
| `strategy_activations` | activation_id、激活/回退类型、前后 revision/hash、管理员和时间 |
| `api_usage` | API 状态、耗时和 credits |
| `discovery_snapshots` | 筛选前特征、REST/G1 来源、顶部池映射、逐池完整规范化决策特征、窗口元数据、字段完整度和采集版本 |
| `discovery_schedule` | 每条链每个查询模板的 `next_due_at` 持久化（重启恢复调度状态） |
| `wallet_observations` | 每个候选最多 20 个钱包的策略无关早期买入事实、精确池、价格、时间和来源 |
| `wallet_outcome_labels` | 观察 ID、策略 hash、`SUCCESS/FAILURE/NEUTRAL/DATA_GAP`、触发时间和计算证据 |
| `wallet_reputation` | 按链、策略 hash 和计算时点重建的钱包样本数、覆盖率、成功/失败/中性、归零/撤池暴露和 LOCAL_SMART 状态 |
| `outcome_snapshots` | 15m/1h/6h/24h 价格、区间高低点、最低流动性和结果标签 |
| `historical_ranges` | 历史 OHLCV 缓存范围和状态 |
| `backtest_runs` | 回测输入、run_id、指标和结论 |

订单、仓位与风险审计表属于交易插件命名空间，核心不建、不读。

### 9.4 数据保留

- 不保存 CoinGecko/GoPlus 完整业务响应或 WebSocket 原始消息。
- 不保存 G1/G2 原始消息；候选 G3 1 秒数据只在内存组成短周期特征。候选和信号决策快照必须保存该 revision 实际读取的完整规范化特征向量，包括 5/10 秒 G3 特征、m5/m15 服务端聚合、两次流动性响应、持仓结构、聪明钱、所有硬门禁和防追高输入，并保存每个值的窗口边界、样本数、供应商明确提供的源时间、`requested_at/received_at`、来源、单位和完整度；供应商未提供源时间时不得用本地时间冒充，不得只保存部分时间窗。
- 筛选前候选只保存重建决策需要的规范化字段、来源和字段可用性。
- 每个候选只保存最近窗口按买入金额排序的前 20 个唯一早期钱包观察，不保存全部 Pool Trades。未进入正式评估的观察事实保留 30 天；`wallet_outcome_labels` 与候选决策时的信誉快照长期保存；可重建事实清理后，未来策略不得用旧聚合结果冒充新参数的结果。
- 普通运行期 1m 行情保留 72 小时。
- 历史回补行情从最后一次回测后保留 7 天。
- 候选决策快照保留 30 天；结果标签（15m/1h/6h/24h 价格、区间高低点、最低流动性、撤池和数据完整度）长期保存。
- 安全快照保留 30 天，信号保留 180 天；策略快照、策略激活事件、回测结论和严重运行事件长期保存。
- `telegram_updates` 明细保留 90 天；无论机器人停机多久，都必须保留最新已提交 `update_id` 对应的 offset checkpoint，只有更高 offset 提交后才能替换，清理不得删除唯一恢复点。
- `telegram_confirmations` 已消费或已过期记录保留 90 天；未消费且未过期记录保留到过期后再开始计算 90 天。
- `telegram_outbox` 的 `PENDING/SENDING/DELIVERY_UNKNOWN` 记录在进入确定终态前不得清理；信号投递的确定终态记录与父信号同样保留 180 天，其他普通通知的确定终态记录保留 90 天。
- `api_usage` 明细保留 30 天，随后按接口和日期汇总；汇总保留 1 年。一般运行事件保留 90 天。
- 历史数据被清理后，范围标记 `REMOVED`，关联回测标记 `DATA_REMOVED`。
- OHLCV 唯一键为 `chain + ohlcv_kind + entity_address + token_side + timeframe + candle_timestamp`。`ohlcv_kind` 为 `POOL/TOKEN`；POOL 使用池地址及 `base/quote`，TOKEN 使用代币地址及 `N/A`。跨策略只共享完全相同的序列。

### 9.5 容量与备份

- 数据卷达到 70% 告警，80% 停止历史回补，90% 停止新信号生成。
- 任何阈值下都继续已有候选监控、结果标签追踪、信号事件发布和关键审计。
- 使用 SQLite Backup API 备份 `bot.sqlite`，定期执行恢复测试。
- 大量清理后执行 WAL checkpoint 和增量 vacuum；完整 VACUUM 只在维护窗口执行。

---

## 10. 回测与策略激活

### 10.1 数据准备

回测分为数据准备和纯本地计算：

1. 检查候选逐池决策快照、策略无关钱包观察事实和结果行情覆盖率。
2. 目标策略按自身参数和每个历史决策时间重建钱包结果标签与信誉；禁止直接复用其他策略的聚合信誉。
3. 先使用本地固定结果点筛选差异样本；只为本次策略变更涉及的样本生成历史 OHLCV 回补计划。
4. 显示链、池、时间范围、粒度和请求批次，不计算或展示套餐消耗结论。
5. 回补前展示 `/key` 当前余额；管理员知情确认后才请求历史接口。
6. 写入 `bot.sqlite` 后，回测阶段断开外部 API。

历史接口只能补充结果行情，不能重建过去的热门候选、G1 顶部池映射、安全状态、短周期微观结构、聪明钱流向或决策时缺失字段。只有目标策略实际读取的全部决策特征在当时快照中完整存在的样本才能参与完整回放；完整本地范围重复回测不得调用外部 API。

精确回放优先使用 Pool OHLCV：`timeframe=minute`、`aggregate=1`、`limit=1000`、`currency=usd`、`token=<目标代币地址>`、`include_empty_intervals=false`，并用 `before_timestamp` 向前分页。`before_timestamp` 请求参数固定为 Unix UTC 秒整数，禁止直接发送内部毫秒值；响应 K 线时间戳按 UTC 秒解析，在 client 归一化层乘 1000 转为内部 UTC 毫秒。下一页固定使用 `本页最早原始秒时间戳 - 1`；空页结束分页，返回非秒级时间戳、时间倒退或重复边界时标记该范围 `FAILED`。每页和 6 个月单次范围限制都进入请求计划；Token OHLCV 因使用当时最活跃池且可能切换池，只能作为 `PARTIAL` 数据。接口跳过的无成交分钟保持为缺口，分析指标可以在内存生成带 `synthetic=true` 的零成交延续条，但该条不能作为入场或退出成交依据。

历史范围状态：

```text
PLANNED / COMPLETE / PARTIAL / FAILED / REMOVED
```

### 10.2 1m 成交模型

回测使用确定性 1m 成交模型：

1. 信号发生在某分钟内时，以下一根真实完整主池 1m K 线开盘价为唯一入场参考；该价格相对信号价的偏差超过 `backtest_model.fill_deviation_pct_max` 时记 `NOT_FILLED`。入场意图必须在信号有效期内提交，提交后不再次检查信号有效期；回测只能在 `fill_deadline_seconds`（由 `backtest_model` 定义，`>= 180`）前取得该参考 K 线，不能改用更晚 K 线。
2. 退出触发后，以下一根完整 1m K 线开盘价为退出参考。
3. 同一 K 线同时触及止盈、止损或追踪止损时，按导致最低卖出价格的全仓保护性退出计算；同一 K 线只触发多个止盈档位时，合并为下一根 K 线的一笔卖单，卖出比例为这些档位之和。
4. 买入价格向不利方向增加滑点和冲击，卖出价格向不利方向减少。
5. 扣除池手续费、税率/转账费和网络费。
6. 主池和下一根真实完整 K 线存在，但流动性、费用或滑点明确不满足时记 `NOT_FILLED`；缺少主池、真实 K 线、费用或流动性数据时记 `DATA_GAP`，不得伪装成未成交。
7. 所有计算使用 `Decimal`。

回放中出现 `DATA_GAP` 时，该信号不产生猜测退出价，回放完整度降级；`synthetic=true` 的 K 线只能维持指标时间轴，不能触发成交、止盈、止损或追踪止损。

冲击公式：

```text
impact_bps = min(
  max_impact_bps,
  impact_coefficient × order_usd / reserve_usd × 10000
)
total_adverse_bps = base_slippage_bps + impact_bps
```

`total_adverse_bps / 100` 必须不高于回测模型对应的入场/退出 pct 上限，否则该根 K 线不成交。普通退出未成交时模拟仓位保持打开并在下一根真实 K 线重新评估。回测中的退出阈值（止盈/止损/追踪止损）由回测配置定义，与交易插件的实盘 TP/SL 参数相互独立。回测中的模拟仓位只存在于回测计算过程，不落库、不发布事件、不属于仓位管理。

### 10.3 回测身份

```text
run_id = SHA-256(canonical_json([
  strategy_hash,
  dataset_hash,
  backtest_engine_version
]))
```

`dataset_hash` 覆盖 REST/G1 来源、顶部池映射、逐池完整规范化决策特征及其窗口元数据和完整度、策略无关钱包观察、目标策略物化的钱包结果标签与决策时信誉、聪明钱流向、决策时间、源数据时间、OHLCV 类型、实体地址、币种方向、1m 结果 OHLCV、合成标记、费用、税率、滑点、完整度和样本切分边界。相同 run_id 必须产生相同结果。

### 10.4 数据完整度

| 状态 | 规则 |
|---|---|
| `COMPLETE` | 候选范围、REST/G1 池证据、策略实际读取的逐池微观特征、该策略按决策时间重建的钱包信誉、聪明钱状态及运行时采取的缺失动作、精确决定池真实 1m 结果 OHLCV 完整，且所有输入的源时间不晚于决策时间；已保存 `TRUNCATED/UNKNOWN` 及其 `ZERO_SCORE/不阻挡` 结果时仍属于决策可完整复现 |
| `PARTIAL` | 决策本身可读取，但缺少运行时实际使用的输入或使用 Token OHLCV、较粗粒度结果行情，无法完整复现运行决策或成交结果 |
| `INVALID` | 缺少历史候选、策略所需微观/钱包关键决策字段、精确池身份或存在时间穿越 |

供应商原始数据完整度与回放完整度分别保存：前者记录 `COMPLETE/TRUNCATED/UNKNOWN`，后者使用本节 `COMPLETE/PARTIAL/INVALID`，不得因为供应商截断但运行决策已确定而自动降低回放完整度。`INVALID` 样本不得用于激活依据，`PARTIAL` 只能作参考。

### 10.5 回测报告

Solana、BSC 分别输出：

- 策略配置差异和优化目标
- 候选数、信号数、通过率和 1m 回放覆盖率
- 净期望值、盈亏比、Profit Factor、最大回撤和连续亏损
- 15m、1h、6h、24h 表现
- 推送即见顶、归零、撤池、安全错误放行和无法执行比例
- 热门、异动、新池标签表现
- 数据缺失、历史请求数和缓存命中率

指标定义固定（仅作为报告指标，不设自动门槛，由管理员人工判断）：

- `candidate_count`：通过快照 Schema 的不同 `chain + token_address + decision_timestamp` 决策样本数，不是通过筛选后的数量；字段值缺失仍保留样本并执行策略的 `missing_action`，不能通过删除缺失样本提高结果。
- `signal_count`：完成历史安全门禁、防追高和评分后产生的 BUY_SIGNAL 数；回测不依赖 Telegram 在线状态。
- `net_expectancy_pct = 100 × Σnet_pnl_usd / Σplanned_order_usd`，分母包含全部 BUY_SIGNAL；确定未成交和无法成交的信号净损益为 0，数据缺口信号不进入收益计算并降低覆盖率。
- `max_drawdown_pct`：以回测初始权益为基准，按事件时间执行仓位回放，权益峰值到后续谷值的最大跌幅。
- `immediate_peak`：信号后 60 分钟最高价出现在前 5 分钟，且此后 60 分钟内从该高点下跌至少 30%；`immediate_peak_ratio_pct` 是完整 60 分钟结果信号中的占比。
- `signal_coverage_pct`：具有完整 15m/1h/6h/24h 结果标签的信号占全部信号比例。`replay_coverage_pct`：具有从决策特征窗口到确定退出所需全部真实主池 1m 数据的信号占全部信号比例。
- "归零"固定指信号后 24 小时最低有效价格不高于信号价的 10%；"撤池"固定指主池流动性不高于决策时的 10%；"安全错误放行"指 24 小时内出现可由决策时已保存安全字段判定、却被该策略放行的硬风险；"无法执行"只统计数据完整但被费用、流动性、滑点或冲击规则判定为 `NOT_FILLED` 的信号，`DATA_GAP` 单独报告。

### 10.6 激活与回退

```text
编辑 strategy.yaml
→ Pydantic Schema 校验
→ 生成相对 ACTIVE 的差异
→ 检查/准备本地数据
→ 计算目标策略回测报告
→ 管理员查看报告并确认
→ SQLite 事务归档旧 ACTIVE 并写入新 ACTIVE
→ 原子替换进程内策略
```

- 普通激活的 `revision` 必须从未被任何策略快照使用，`parent_revision` 必须等于当前 ACTIVE 的 revision；基准初始化时两者分别为 `strategy-1` 和 `null`。
- Schema 校验后对完整配置生成确定性 canonical JSON，并保存 `strategy_hash=SHA-256(canonical_json)`；revision 与 hash 共同写入策略快照和信号。
- 激活不设自动晋升门槛：管理员在查看回测报告（见 10.5）后自行决策；报告仅提供事实与指标，不做出激活/拒绝结论。
- 激活失败时保持原 ACTIVE 不变。未激活文件不影响运行或重启。
- 回退只允许选择已成功激活且哈希完整的不可变历史快照。管理员确认后，重新激活该快照原有的 revision/hash，并原子写回唯一 `strategy.yaml`；不得为回退伪造新 revision。每次普通激活和回退另建唯一 `activation_id` 审计事件。回退不要求历史行情仍然存在。
- 运行时不并行执行影子策略，不产生影子信号。
- `/strategy validate-collection`：对未激活策略文件（或当前 ACTIVE）的 collection 查询模板执行 dry-run 请求，报告每链、每模板的采集覆盖率、错误与延迟；不调用候选详情、GoPlus、G1/G3、Telegram，不执行安全、评分、信号和事件发布。dry-run 结果及其输入哈希长期审计，临时候选键保留 7 天后删除。管理员可以取消；取消或失败均恢复 ACTIVE 正常发现。
- 新 ACTIVE 只影响后续候选与信号。

---

## 11. 故障、安全与运行控制

### 11.1 故障行为

| 故障 | 行为 |
|---|---|
| CoinGecko REST 不可用 | 停止发现和新信号生成，已有候选与结果标签追踪继续 |
| CoinGecko WebSocket 断开 | 暂停断线链实时信号，按 4.5 的退避与抖动重连并从 SQLite 恢复订阅 |
| GoPlus 不可用 | 禁止没有有效快照的新信号，结果标签追踪继续 |
| Telegram 不可用 | 禁止需要推送的新信号；PENDING 按 8.3 排队补发且不改变原信号有效期 |
| SQLite 写入失败 | 停止发现和新信号生成，关键事务等待同一事务并告警 |
| 磁盘不足 | 停止历史回补和新信号生成，保留结果标签追踪与审计 |
| 策略文件无效 | 继续使用最后有效 ACTIVE |
| 系统重启 | 恢复候选/信号状态、订阅、额度窗口、结果标签追踪计划与插件注册 |

启动恢复必须先于发现和新信号：加载未终态 outbox 与结果标签追踪计划，恢复今日 WS 计费计数与 REST 并发状态，恢复 G1/G3 订阅，重新加载插件。插件加载失败按第 15 章隔离处理，不影响核心恢复。

### 11.2 Secret 与权限

运行环境必须提供：

- CoinGecko API Key
- Telegram Bot Token、管理员 ID、信号频道 ID

GoPlus 安全接口按无 Authorization 方式调用，不配置 GoPlus Token。Secret 不进入 Git、日志、Telegram 消息或 SQLite 业务表。Telegram 进程不保存链上私钥。插件凭证（如 DBotX Key）由插件自持，见各插件文档，核心不配置、不读取。

---

## 12. 代码结构与开发顺序

### 12.1 固定代码结构

```text
app/core/
├── main.py
├── config.py
├── models.py
├── enums.py
├── backtest.py
├── clients/
│   ├── coingecko.py
│   ├── coingecko_ws.py
│   ├── goplus.py
│   └── telegram.py
├── services/
│   ├── discovery.py
│   ├── security.py
│   ├── wallet_intelligence.py
│   ├── strategy.py
│   ├── outcome_tracking.py
│   ├── usage.py
│   ├── admin.py
│   └── maintenance.py
├── bus/
│   ├── events.py
│   ├── notify.py
│   └── commands.py
├── storage/
│   ├── database.py
│   ├── repository.py
│   └── migrations/
└── tests/

plugins/                    # 预留目录；插件包约定见插件 SDK 规范
├── trade/
└── swing/

config/
├── strategy.yaml
└── plugins.yaml
```

- 不使用 ORM，SQL 集中在 Repository 层。
- 外部响应只在 clients 层解析。
- 评分、安全、聪明钱和回测核心规则使用纯函数。
- 回测器不得依赖外部 API client。
- 插件只通过 `bus/` 三个接口与核心交互，不得直接 import 核心业务模块（细节见第 15 章与插件 SDK 规范）。

### 12.2 开发顺序

1. 固化 CoinGecko、GoPlus client 和响应模型。
2. 建立 `bot.sqlite`、迁移、优先级写入和恢复。
3. 实现统一发现、三标签、主池、安全门禁和缓存。
4. 实现 G1 顶部池 WATCH、Tokens Multi 池映射、G3 `1s` FOCUS、WS 每日预算、每频道 100 订阅上限、精确决定池聚合和重连；G2 只保留能力探测。
5. 实现双链策略、盈利交易者/本地钱包信誉、信号事件、Telegram 信号。
6. 实现结果标签追踪。
7. 实现历史数据准备、1m 回测、手动激活和回退。
8. 完成容量、故障、恢复测试与验收。

---

## 13. 测试与验收

### 13.1 单元测试

- 双链地址、Decimal、时间和 API 字段归一化
- Unix UTC 秒 `before_timestamp` 与内部 UTC 毫秒双向转换、下一页最早秒减 1、空页结束、重复边界和错误单位拒绝
- GoPlus 条件省略、UNKNOWN、NOT_APPLICABLE、`fake_token.value` 对象解析和主池匹配
- Solana 按 owner account 汇总多个 token account；双链 Top 10 只排除明确 burn/zero、精确主池和已验证锁仓地址
- 双链只允许规定池保护模型发信号，其他池模型保持观察且不得提供伪造 LP 分数
- 安全字段出现 RISK、UNKNOWN 或 STALE 时整体不得发信号
- CoinGecko 详细检查和 GoPlus PRE_MONITOR_CHECK 必须先于候选 G3 订阅；PRE_EXECUTION_CHECK 只在信号落库前执行
- G3 1 秒事件去重、2 秒 final、连续 5/10 秒本地聚合、秒级缺口拒绝、更早K线不得补位、连续第二轮窗口、迟到修正和已保存决策不可变
- G1 顶部池映射、REST/G1 池地址去重、统一筛选和同代币单信号；未经池绑定的 G1 价格不得进入评分，G2 运行订阅必须拒绝
- Multiple Pools 的 m5/m15 成交量、交易数和参与者加速度公式，滚动快照差值不得伪装成精确 10 秒笔数
- Multiple Pools 无源时间时只保存本地请求/接收时间，相同响应记 `OBSERVATION_LIMITED`，不得证明流动性稳定
- Multiple Pools `buy_volume_usd/sell_volume_usd/net_buy_volume_usd` 归一化与保留；`include_volume_breakdown=true` 依赖关系按契约测试结论锁定
- CoinGecko holder 源时间 TTL、未来时间拒绝、至少一个地址级来源有效、双源有效时 Top 10 取最大值以及 holder count 不作硬门禁
- Top Holders 最终 query 必含 `include_pnl_details=false`，Top Traders 最终 query 必含 `include_address_label=false`，业务模型不得读取被关闭的扩展字段
- Solana Token Info `is_honeypot="unknown"` 归一化为带原因的 NOT_APPLICABLE，其他类型或值触发契约变化 UNKNOWN；BSC 仍执行布尔门禁
- Top Traders/Pool Trades 地址匹配、排除集合、返回上限边界同时间截断、时间解析失败、LOCAL_SMART 最小样本、按链隔离和聪明钱卖出阻挡
- 供应商聪明钱 `TRUNCATED/UNKNOWN` 与回放完整度分离；保存实际 `ZERO_SCORE/不阻挡` 决策后仍可确定性重放
- 全部派生特征、Decimal 精度、分母为零、窗口端点、平盘 K 线、G3 VWAP、高位整理、回踩恢复和流动性下降公式
- 钱包观察的同分钟排除、首次触发顺序、`SUCCESS/FAILURE/NEUTRAL/DATA_GAP`、按策略 hash 和历史决策时间重建信誉以及参数变化不得复用旧分类
- Pool/Token OHLCV、base/quote 方向的唯一键隔离
- 三标签合并和信号去重
- signal_generation 重启恢复、SETUP 确认与重新武装时长隔离、持续满足条件不重发
- 双链可信报价地址校验、假同名报价代币拒绝和主池变更后特征不得跨池复用
- 评分公式、非负权重、特征方向、除零拒绝、`REJECT/CAP_SETUP/ZERO_SCORE` 缺失动作和防追高
- 每项评分特征的独立取值域、计数阈值允许 0、有符号特征允许负数，以及 NaN/Infinity 拒绝
- 信号事件 Schema v1 校验：必填字段、版本拒绝、signal_created/signal_invalidated 触发条件与载荷一致性
- 1m 成交模型、同 K 线止盈止损、费用和舍入（回测语境）
- 信号有效期只约束回测入场意图提交、`fill_deadline_seconds`、补洞不得改用更晚 K 线
- strategy.yaml Schema、双链隔离、revision/hash 唯一规则、collection 内容变化必须递增版本和 ACTIVE 唯一约束；执行/风险/晋升字段必须被拒绝
- 探测报告元数据校验、无版本旧报告归类为 LEGACY_EVIDENCE、旧报告不得单独支持新增或变更契约
- run_id 确定性

### 13.2 集成测试

- CoinGecko Analyst REST、G1 ActionCable `set_tokens`、G2 逐条计费和 G3 `set_pools`/成功确认契约
- Top Holders `holders=40/50&include_pnl_details=false`、Top Traders `traders=20&include_address_label=false`、Pool Trades 目标代币参数、Pool OHLCV UTC 秒 `before_timestamp` 连续分页与内部毫秒归一化
- 固定双链两个 WebSocket 连接、每连接每频道不超过 100 个订阅、单链独立退避/抖动/重连/恢复、稳定连接后重置退避以及鉴权失败停止重试
- G1 按代币、G3 按精确池分别执行 100 订阅物理上限；每订阅阶段最大消息数、多池与第二轮独立计费
- `/key` 精确字段、15 分钟轮询、余额告警阈值；WS 每日预算计数、重启保留当日计数与超预算停止新阶段；REST 全局并发上限；配置与代码不得包含套餐月额度或月度消耗推算
- GoPlus 双链无 Authorization 调用和 30 次/分钟限流
- Telegram Long Polling update_id/offset 持久化、管理员权限、outbox delivery_key、PENDING 恢复自动补发、SENDING 恢复转 DELIVERY_UNKNOWN、明确失败补发和 DELIVERY_UNKNOWN 不自动补发
- Telegram 一次性 nonce、callback 绑定、60 秒过期、重复 update/callback 幂等
- 信号事件发布：信号落库事务与事件发布原子一致、重启不重复发布、事件版本校验拒绝未知版本
- `bot.sqlite` WAL、并发、备份和恢复
- 高候选和多池输入下的 G1/G3 订阅准入（WS 每日预算）、REST 并发上限与关键事务优先写入
- 候选订阅淘汰、结果标签追踪定点查询与补洞
- 外部 API 断开时的纯本地回测
- 完整历史缓存命中时不得调用外部 API，REMOVED 范围重新生成计划
- 历史回补前展示 `/key` 当前余额并由管理员知情确认，且不得依赖文档或配置中的固定套餐消耗公式
- Pool OHLCV 1000 条分页、目标币方向、真实空档与 synthetic 指标条隔离、`DATA_GAP` 不计作未成交
- 重启恢复候选状态、额度窗口、订阅与结果标签追踪计划
- 无历史行情时回退已激活策略快照
- 普通激活 revision 不复用且 parent 指向当前 ACTIVE；回退复用原 revision/hash 并生成独立 activation_id
- collection validate dry-run 只保存键、无深查/信号/事件、取消恢复和临时键清理
- 数据保留、按日汇总、清理和容量熔断
- Telegram updates 最新 offset 永不误删、confirmation 活跃记录保护、outbox 未终态保护及按父业务类型分层清理

### 13.3 验收条件

1. 系统保持一个 Python 进程、一个 `strategy.yaml` 和一个 `bot.sqlite`。
2. CoinGecko、GoPlus 严格遵守本文档职责边界；核心不持有、不读取任何交易凭证。
3. Solana、BSC 策略完全分区，运行时只有一个整体 ACTIVE。
4. 每条链只有一个采集调度器；各模板按独立 `next_due_at` 运行，Megafilter client 注入三项采集门禁参数；三个发现标签不重复深查、安全检查、评分、信号或事件。
5. 安全危险或未知时禁止产生信号。
6. 候选 G3 前必须通过 CoinGecko 详细检查和 GoPlus PRE_MONITOR_CHECK；信号落库前通过 GoPlus PRE_EXECUTION_CHECK，再发布事件并推送 Telegram。
7. 信号事件按 Schema v1 发布，`signal_created`/`signal_invalidated` 触发条件与载荷完整。
8. 数据保留、容量熔断、Secret 和管理员权限符合本文档。
9. 配套 `strategy.yaml` 包含双链全部必填值，Schema 不接受缺失字段、未知字段、隐式默认值、Megafilter 重复映射参数、非法特征值域、非法评分方向以及任何执行/风险/晋升字段。
10. 每链单 WebSocket 连接；G1/G3 各 100 物理上限、G2 不进入运行策略；热门与异动不少于 90% 容量、新池最多 10%。
11. 结果标签追踪不占用 G1/G3 订阅，定点查询计入 REST 并发上限。
12. 候选订阅不会被候选外的任何主体挤占。
13. Pool/Token 与 base/quote OHLCV 不发生覆盖，已激活历史策略可在无行情时回退。
14. 回测只用真实主池 1m K 线成交，Pool OHLCV 分页请求使用 UTC 秒且入库统一为 UTC 毫秒，数据缺口不伪装为未成交；相同 run_id 结果一致。
15. 主池报价代币必须命中对应链策略中的精确可信地址；不存在可信报价池时不得发信号。
16. 聪明钱明确区分 TOKEN_PROFITABLE 与 LOCAL_SMART；截断或未知数据不得产生买入加分或卖出阻挡，钱包信誉按链隔离且达到最小样本后才生效。
17. 全部评分与防追高派生特征遵守唯一公式、窗口、Decimal 和缺失规则，实时与回测使用同一实现。
18. 普通激活与回退遵守各自唯一且互斥的 revision/hash/activation_id 规则，激活为管理员手动决策，无自动门槛。
19. Telegram Long Polling 从持久化 offset 恢复并按 `update_id` 去重；高风险命令使用绑定参数的一次性确认 nonce；outbox 以 `delivery_key` 幂等，PENDING 恢复补发不改变原 `expires_at`，过期补发标记"已过期，仅通知"，DELIVERY_UNKNOWN 不得自动补发。
20. Telegram 三张表按 9.4 的独立周期清理，最新 offset checkpoint、未终态 outbox 和未过期 confirmation 不得被一般事件清理任务删除。

---

## 14. 官方接口参考

### CoinGecko

- [Endpoint Overview](https://docs.coingecko.com/reference/endpoint-overview)
- [REST/WebSocket Credit Model](https://docs.coingecko.com/docs/data-delivery-methods)
- [Pools Megafilter](https://docs.coingecko.com/reference/pools-megafilter)
- [Trending Pools](https://docs.coingecko.com/reference/trending-pools-network)
- [Multiple Pools](https://docs.coingecko.com/reference/pools-addresses)
- [Tokens Multi / Top Pool Mapping](https://docs.coingecko.com/reference/tokens-data-contract-addresses)
- [Token Info](https://docs.coingecko.com/reference/token-info-contract-address)
- [Top Token Holders](https://docs.coingecko.com/reference/top-token-holders-token-address)
- [Top Token Traders](https://docs.coingecko.com/reference/top-token-traders-token-address)
- [Pool Trades](https://docs.coingecko.com/reference/pool-trades-contract-address)
- [Pool OHLCV](https://docs.coingecko.com/reference/pool-ohlcv-contract-address)
- [Token OHLCV](https://docs.coingecko.com/reference/token-ohlcv-token-address)
- [OnchainSimpleTokenPrice G1](https://docs.coingecko.com/websocket/onchainsimpletokenprice)
- [WebSocket Limits](https://docs.coingecko.com/websocket)
- [OnchainTrade G2](https://docs.coingecko.com/websocket/onchaintrade)
- [OnchainOHLCV G3](https://docs.coingecko.com/websocket/onchainohlcv)
- [API Usage](https://docs.coingecko.com/reference/api-usage)

### GoPlus

- [API Overview](https://docs.gopluslabs.io/reference/api-overview)
- [EVM Token Security Response](https://docs.gopluslabs.io/reference/response-details)
- [Solana Token Security API](https://docs.gopluslabs.io/reference/solanatokensecurityusingget)
- [Solana Security Response](https://docs.gopluslabs.io/reference/response-detail-1)

### Python 与 SQLite

- [Python asyncio](https://docs.python.org/3/library/asyncio.html)
- [Python sqlite3](https://docs.python.org/3/library/sqlite3.html)
- [SQLite WAL](https://sqlite.org/wal.html)

---

## 15. 插件总线

核心通过插件总线向插件提供全部交互能力：事件发布、通知、命令路由与生命周期管理。插件不直接访问核心业务模块、Telegram 或数据库核心表。完整的插件开发契约（注册细节、目录约定、迁移机制）见《插件 SDK 规范》（第二阶段交付）；本章锁定核心侧的接口与事件 Schema。

### 15.1 职责边界

| 接口 | 核心职责 | 插件职责 |
|---|---|---|
| 事件订阅 | 在信号生命周期关键节点发布事件，事件与信号落库事务原子一致，重启不重复发布 | 注册处理函数，自行决定行为（开仓、波段、仅记录） |
| 通知 | 统一 outbox 投递与幂等 | 只提交消息文本与目标，不持有 bot token |
| 命令路由 | 管理员校验、update 去重、确认框架 | 声明命令与处理逻辑 |
| 生命周期 | 启动加载、异常包裹、熔断 | 声明式注册、非阻塞实现、自持凭证 |

### 15.2 事件 Schema v1

所有事件载荷为 JSON 对象，`schema_version` 固定为字符串 `"1"`。核心只发布 `signal_created` 与 `signal_invalidated` 两类事件。插件收到高于自身支持版本的事件时必须显式拒绝并告警，不得静默吞掉。

`signal_created` 必填字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | 固定 `"1"` |
| `event_id` | string | 事件唯一 ID（进程内单调 + 重启后与信号状态联动，不重复） |
| `signal_id` | string | 信号唯一 ID，与 SQLite `signals` 表一致 |
| `chain` | string | `solana` / `bsc` |
| `token_address` | string | 目标代币地址 |
| `pool_address` | string | 决定池地址 |
| `signal_level` | string | `BUY` |
| `total_score` | string | 总分（Decimal 十进制字符串） |
| `reference_price` | string | 信号参考价（Decimal 十进制字符串） |
| `security_snapshot` | object | `{pass: bool, goplus_at, coingecko_at}`，安全复检通过时间与结论摘要 |
| `expires_at` | string | 信号有效期截止（UTC 毫秒） |
| `strategy_revision` | string | 产生信号的策略 revision |
| `strategy_hash` | string | 策略 hash |
| `telegram_status` | string | 发布时的投递状态（`PENDING/SENT/FAILED/DELIVERY_UNKNOWN`） |
| `created_at` | string | 信号创建时间（UTC 毫秒） |

`signal_invalidated` 必填字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | string | 固定 `"1"` |
| `signal_id` | string | 被作废的信号 ID |
| `chain` | string | `solana` / `bsc` |
| `token_address` | string | 目标代币地址 |
| `pool_address` | string | 决定池地址 |
| `reason` | string | `EXPIRED` / `POOL_LIQUIDITY_COLLAPSE` / `ADMIN_REVOKE` |
| `invalidated_at` | string | 作废时间（UTC 毫秒） |
| `strategy_revision` | string | 原信号策略 revision |

规则：

- `signal_created` 与信号落库在同一 SQLite 事务中原子提交；进程重启后只对未发布过的信号补发事件，已发布事件不重发。
- 事件投递为**至少一次**语义：插件必须以 `event_id` 幂等处理重复投递（详见插件 SDK 规范）。
- 事件通过内存队列按创建顺序分发；核心对每个插件的处理调用设置分发超时（`dispatch_timeout_seconds`，默认 5 秒），超时或异常均计一次熔断计数并继续分发给后续插件，不阻塞其他插件（见 15.5）。
- 事件载荷不允许包含任何交易凭证、钱包地址或私密配置。

### 15.3 通知 API

插件调用 `notify(text, target, priority)` 提交通知：

- `target` 为管理员会话或信号频道；`priority` 只影响 outbox 调度顺序。
- 通知进入核心 `telegram_outbox`，投递、补发、幂等全部遵循第 8.3 节。
- 插件不得持有 Telegram Bot Token，不得直接调用 Telegram API。

### 15.4 命令路由

插件通过 `commands.register(name, handler, requires_confirmation)` 注册命令：

- 命令名与核心命令冲突时核心拒绝注册并告警（冲突判定在插件加载时执行）。
- 核心负责管理员 ID 校验、`update_id` 去重与确认框架；`requires_confirmation=true` 的命令沿用第 8.3 节的一次性 nonce 确认。
- 命令参数原样分发给插件处理函数；处理函数异常按 15.5 计熔断。

### 15.5 插件生命周期与熔断

- 插件包位于 `plugins/<name>/`，暴露 `register(app)` 入口；核心启动时按 `config/plugins.yaml` 的启用列表顺序加载。
- 插件加载失败只记录告警并跳过该插件，不阻断核心启动；插件没有热加载，启用/停用通过重启生效。
- 插件事件处理函数必须为非阻塞异步实现；核心对同步（阻塞式）注册函数拒绝并告警。
- 核心包裹每个插件的事件处理调用：同一插件连续处理失败或处理超时达到阈值后熔断该插件（停止分发事件与命令）并告警管理员，其他插件与核心不受影响；熔断解除必须通过重启或管理员显式操作。

### 15.6 存储隔离

- 插件使用共享 `bot.sqlite` 中带 `<plugin_name>_` 前缀的命名空间表。
- 核心不读写插件表；插件不读写核心业务表（`tokens/pools/candidates/signals` 等）。
- 插件迁移走独立的版本管理（细节见插件 SDK 规范）；核心迁移与清理任务不触碰插件表。

