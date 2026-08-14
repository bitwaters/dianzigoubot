# plugin-sdk

## ADDED Requirements

### Requirement: 事件投递语义
插件总线 SHALL 以至少一次语义投递事件：事件可能因核心重启补发等原因重复投递。插件 MUST 以 `event_id` 幂等处理重复投递（例如以 `event_id` 唯一约束落库，重复事件直接跳过）。核心 MUST 保证同一事件在单次运行内不重复发布。

#### Scenario: 重复投递幂等处理
- **WHEN** 插件因核心重启补发收到一条已处理过的 `signal_created` 事件
- **THEN** 插件依据 `event_id` 识别重复并跳过，不产生第二次业务动作

### Requirement: 分发超时
核心 SHALL 对每个插件的事件处理调用设置分发超时；处理超过超时或抛出未捕获异常均 MUST 计一次熔断计数，核心 MUST 继续分发给后续插件，不等待慢插件。超时默认值由核心配置（`dispatch_timeout_seconds`，默认 5 秒）。

#### Scenario: 慢插件不阻塞其他插件
- **WHEN** 某插件事件处理超过分发超时仍未完成
- **THEN** 该次处理计熔断计数，后续插件继续收到事件，核心运行不受影响

## MODIFIED Requirements

### Requirement: 插件故障熔断
核心 SHALL 包裹插件的事件处理调用；同一插件在滑动窗口内连续抛出未捕获异常或超过分发超时达到阈值后 MUST 熔断该插件（停止向其分发事件）并告警管理员，MUST NOT 影响其他插件与核心自身。熔断解除 MUST 通过重启或管理员显式操作。

#### Scenario: 连续失败熔断
- **WHEN** 某插件处理事件连续 N 次抛出未捕获异常
- **THEN** 核心停止向该插件分发事件并告警，其他插件不受影响

#### Scenario: 超时计入熔断
- **WHEN** 某插件连续多次处理超时达到阈值
- **THEN** 核心熔断该插件并告警，与异常熔断行为一致
