# LLM Observability Phase 3 最终验收缺口修复需求

> 项目：`xjy486/llm-observability`  
> 审查范围：`7df3ceee605a716f7fb22cc68566cae59cabc2ac` → `3f0994a7b8834877029a92d515e403064fb0b42e`  
> 当前阶段：Phase 3 — Gateway Native Observability  
> 当前结论：Gateway Contract 与 Runtime 主体已经完成，CI 与 Gateway E2E 已通过，但完整 Phase 3 仍存在运行时并发终态、Hedged Attempt 最终胜者语义、One-API 真实接入、Gateway 专用 UI 与 Metrics 等验收缺口。  
> 建议 OpenSpec Change：`fix-phase3-final-acceptance-gaps`

---

# 1. 背景

Phase 3 当前已经实现：

```text
Gateway Contract
Router GATEWAY Span
Provider Attempt GATEWAY Span
Retry / Fallback / Cache / Rate-limit Event
Streaming Wrapper
Usage / Cost 归一化与聚合
PrivacyGuard
Sampling
Fail-open
Generic GatewayAdapter
OneApiAdapter 字段映射
真实 HTTP Harness E2E
Live OpenAI-compatible Endpoint E2E
```

当前标准 Trace 结构为：

```text
AGENT
└── LLM
    └── GATEWAY router
        ├── GATEWAY provider_attempt 1
        └── GATEWAY provider_attempt 2
```

无 SDK 调用时：

```text
GATEWAY router
└── GATEWAY provider_attempt
```

现有实现已经解决：

```text
无 SDK TraceID 生成
SDK / remote / gateway 三类 Trace Origin
Attempt 关闭不清空 Router Context
跨线程 ContextVar 清理
Router 关闭时强制结束遗留 Attempt
关闭后的 Router 拒绝新 Attempt
Attempt Index 并发分配
并行 Attempt Usage / Cost 聚合
Attempt Exactly-once 聚合
Publish-before-claim
Streaming Publish-before-close
Channel ID 哈希与 PrivacyGuard
Gateway Real E2E 0 skipped
```

但完整 Phase 3 Definition of Done 仍未完全满足。

---

# 2. 修复范围

本轮只处理以下内容：

```text
P0-1 Streaming Terminal 原子状态机
P0-2 Hedged / Parallel Attempt Winner 语义
P0-3 One-API 生产接入边界确认
P0-4 Gateway UI 与 Metrics 范围确认和实现
P1-1 Streaming Duration 字段语义统一
P1-2 Terminal Event 互斥组
P1-3 Phase 3 分阶段冻结与版本标记
```

本轮不进入：

```text
Embedding / Retrieval / Rerank
LangGraph Advanced Workflow
MCP Native Observability
Claude Code / Codex 本地事件
Prompt 管理
自动路由优化
自动降级控制
```

---

# 3. P0-1：Streaming Terminal 必须是原子状态机

## 3.1 当前问题

当前 Streaming Finalizer 使用普通布尔值：

```python
self._finalized = False
```

终态入口采用：

```python
if self._finalized:
    return
self._finalized = True
```

对应：

```text
finalize_success
finalize_error
finalize_cancelled
```

该检查和赋值不是原子操作。

可能发生：

```text
线程 A：流正常耗尽，进入 finalize_success
线程 B：客户端同时 close，进入 finalize_cancelled

A 读 finalized=False
B 读 finalized=False
A 写 finalized=True
B 写 finalized=True
```

可能结果：

```text
Router 聚合 success
Attempt 被标记 client_cancelled
Router Record = OK
Attempt Record = ERROR
```

或者同一 Span 同时存在：

```text
gateway.stream.completed
gateway.stream.cancelled
```

这会破坏 Router 与 Attempt 的终态一致性。

---

## 3.2 修复要求

新增原子终态声明：

```python
import threading


class _TerminalFinalizer:
    def __init__(self, ...):
        self._terminal_lock = threading.Lock()
        self._terminal_state = None

    def _claim_terminal(self, state: str) -> bool:
        with self._terminal_lock:
            if self._terminal_state is not None:
                return False
            self._terminal_state = state
            return True
```

终态入口统一为：

```python
def finalize_success(self):
    if not self._claim_terminal("success"):
        return
    ...

def finalize_error(self, error):
    if not self._claim_terminal("error"):
        return
    ...

def finalize_cancelled(self):
    if not self._claim_terminal("cancelled"):
        return
    ...
```

要求：

```text
同一个 Stream 只能有一个 Terminal State
只有赢得 Terminal Claim 的路径可以：
- 写入 Attempt Error / Status
- 聚合 Router
- 记录 Terminal Event
- 关闭 Attempt
- 关闭 Router
```

失败竞争路径必须直接返回，不得再修改 Span 或 Router Aggregate。

---

## 3.3 终态优先级

必须冻结竞争时的优先级语义。

推荐：

```text
业务错误已经发生
> 客户端取消
> 正常完成
```

但不允许通过时间戳猜测。

推荐规则：

```text
第一个成功 claim terminal lock 的终态为最终终态
后续终态全部 no-op
```

若产品要求业务异常优先，则需要明确实现专门的可升级状态机，而不是隐式覆盖。

首版建议采用：

```text
first terminal claim wins
```

---

## 3.4 必须新增的测试

```text
test_stream_exhaustion_racing_close_claims_one_terminal
test_stream_error_racing_close_claims_one_terminal
test_done_marker_racing_disconnect_claims_one_terminal
test_async_cancel_racing_aclose_claims_one_terminal
test_stream_terminal_records_one_stream_event
test_stream_terminal_records_one_attempt_event
test_stream_terminal_records_one_router_event
test_stream_terminal_router_attempt_status_consistent
test_stream_terminal_aggregates_exactly_once
```

每个测试必须使用：

```text
threading.Event
threading.Barrier
asyncio.Event
```

制造确定性的竞争窗口，禁止依赖 `sleep()` 猜测时序。

---

# 4. P0-2：Hedged / Parallel Attempt Winner 语义

## 4.1 当前问题

Router 当前会在每次 Attempt Result 聚合时覆盖：

```text
final_channel_id
final_http_status
final_error
```

这实际上意味着：

```text
最后完成的 Attempt 决定 Router 最终状态
```

串行 Retry 场景通常成立：

```text
Attempt 1 失败
Attempt 2 成功
→ Attempt 2 是最终结果
```

但 Hedged / Parallel Provider 场景不成立：

```text
Attempt 1 成功，结果已经返回用户
Attempt 2 是并行 loser，稍后 timeout
```

若按完成顺序覆盖：

```text
Router 最终可能被标记 ERROR
final_channel 可能变成 loser channel
final_error 可能是 loser timeout
```

但真实业务结果已经由 Attempt 1 成功完成。

---

## 4.2 必须冻结的所有权

必须区分：

```text
所有 Attempt Aggregate
业务最终 Winner
```

所有 Attempt Aggregate 用于：

```text
Usage
Cost
success_count
fail_count
attempt_count
retry_count
fallback_count
```

Winner 用于：

```text
Router final status
Router final channel
Router final HTTP status
Router final error
Router finish reason
```

---

## 4.3 推荐数据模型

在 `AttemptResult` 增加：

```python
@dataclass
class AttemptResult:
    attempt_index: int
    ...
    selected: bool = False
```

或者由 Router 显式选择：

```python
router.select_winner(attempt_index=1)
```

推荐第二种，业务语义更明确：

```python
router.select_winner(
    attempt_index=attempt.attempt_index,
    reason="first_success",
)
```

Router 内部增加：

```text
_selected_attempt_index
_selected_result
_selection_reason
```

---

## 4.4 推荐规则

串行 Retry：

```text
最后一个被业务接受的 Attempt 为 Winner
```

Hedged Request：

```text
第一个被业务层采用并返回给调用方的 Attempt 为 Winner
```

Fallback：

```text
Fallback 后成功的 Attempt 为 Winner
```

全部失败：

```text
由网关业务层选择最终对外返回的失败 Attempt
```

未显式选择时：

```text
禁止依赖“最后完成”
```

推荐 Fail-safe：

```text
无 Winner 且只有一个 Attempt
→ 自动选择该 Attempt

无 Winner 且多个 Attempt
→ Router final status = ERROR
→ gateway.final_error_category = gateway_internal
→ gateway.final_error_type = MissingWinnerSelection
```

或者显式禁止并行 Attempt。

---

## 4.5 Router API

建议新增：

```python
def select_winner(
    self,
    attempt_index: int,
    reason: str | None = None,
) -> bool:
    ...
```

规则：

```text
只允许选择已激活 Attempt
只允许选择已经有 AttemptResult 的 Attempt
默认只允许设置一次
重复选择同一个 Attempt幂等
尝试切换 Winner 必须记录事件
```

新增事件：

```text
gateway.attempt.selected
```

属性：

```text
attempt_index
channel_id
provider
reason
```

Channel ID 必须哈希。

---

## 4.6 必须新增的测试

```text
test_hedged_success_then_loser_timeout_router_stays_ok
test_hedged_loser_timeout_then_success_router_ok
test_selected_attempt_defines_final_channel
test_selected_attempt_defines_final_http_status
test_selected_attempt_defines_final_error
test_parallel_usage_includes_all_attempts
test_parallel_cost_includes_losing_attempts
test_select_winner_is_idempotent
test_select_unknown_attempt_rejected
test_multiple_attempts_without_winner_is_deterministic
```

---

# 5. P0-3：One-API 生产接入

## 5.1 当前问题

当前 `OneApiAdapter` 已实现：

```text
Request → GatewayRequestContext
Channel → RouteDecision
State → AttemptContext
Response → Usage
Exception → GatewayError
```

但尚未看到真正的 One-API 生命周期接入：

```text
请求进入 One-API
路由选择完成
真实上游 Attempt 开始
真实 Retry/Fallback
StreamingResponse 生命周期
Quota/Usage 返回
```

当前测试主要是人工构造 Python 字典，并手动调用：

```python
attempt.start()
handle.finish_attempt(...)
handle.retry_scheduled(...)
```

这只能证明 Mapping Adapter 正确，不能证明 One-API Production Integration 已成立。

---

## 5.2 必须二选一

### 方案 A：完成 One-API Production Integration

新增真正的胶水层：

```text
One-API Middleware / Hook / Sidecar
→ OneApiAdapter
→ GatewayRuntime
```

至少支持：

```text
Request Start
Route Selected
Attempt Start
Attempt Complete / Failed
Retry
Fallback
Streaming
Request Finalize
```

并新增真实或容器化 One-API E2E。

### 方案 B：正式拆分阶段

将当前成果冻结为：

```text
Phase 3.0 — Gateway Observability Contract & Runtime
✅ COMPLETE
```

另立：

```text
Phase 3.1 — One-API Production Integration
```

该阶段负责真实 One-API 接入。

推荐方案 B，因为当前 Runtime 已经相对稳定，而 One-API 接入是独立的跨项目集成工作。

---

## 5.3 若选择方案 A

建议目录：

```text
integrations/oneapi/
├── adapter.py
├── middleware.py
├── hooks.py
├── lifecycle.py
├── streaming.py
└── bootstrap.py
```

接入边界：

```text
不得修改 One-API 路由结果
不得修改重试次数
不得修改超时
不得修改 Quota
不得吞掉业务异常
Telemetry 失败必须 Fail-open
```

---

## 5.4 真实 One-API E2E

至少覆盖：

```text
One-API 一次成功
One-API 500 → Retry → 200
One-API Channel Fallback
One-API Streaming Success
One-API Client Cancel
One-API Sampling=0
One-API Privacy
```

硬断言：

```text
真实 One-API Route/Retry/Fallback 事件驱动 Runtime
不是测试代码手动调用 Runtime
Router / Attempt 父子关系正确
Channel ID 全部哈希
Usage / Cost 正确
无 Registry / Context 泄漏
```

---

# 6. P0-4：Gateway UI 与 Metrics

## 6.1 当前问题

Phase 3 原需求包含：

```text
Router Detail
Attempt Timeline
Route Decision
Cost Breakdown
Gateway Filters
Gateway Metrics
Retry Waste Cost
```

当前通用 Trace Tree 可以展示 GATEWAY Span，但没有证据表明已实现 Gateway 专用视图与指标。

因此必须二选一：

```text
实现 Phase 3 UI / Metrics
```

或者：

```text
把 UI / Metrics 正式移出 Phase 3.0 DoD
```

---

## 6.2 Gateway UI 最小范围

### Router Detail

显示：

```text
Gateway Name
Protocol
Requested Model
Resolved Model
Provider
Final Channel
Route Reason
Retry Count
Fallback Count
Attempt Count
Cache Status
Total Duration
TTFT
Total Usage
Total Cost
Final Status
Final Error
```

### Attempt Timeline

按时间顺序显示：

```text
Attempt Index
Provider
Hashed Channel ID
Resolved Model
Start / End
Duration
TTFT
HTTP Status
Usage
Cost
Error
Selected Winner
```

### Route Decision

显示：

```text
requested_model → resolved_model
route_reason
policy_name
fallback from/to
retry reason
cache status
rate-limit status
```

### Cost Breakdown

显示：

```text
Winner Attempt Cost
Failed Attempt Cost
Losing Hedged Attempt Cost
Retry Waste Cost
Total Cost
```

---

## 6.3 Gateway Filters

至少支持：

```text
gateway.provider
gateway.channel_id
gateway.requested_model
gateway.resolved_model
gateway.error_category
gateway.span_role
gateway.cache_status
```

禁止用以下字段作为低级 Metrics Label：

```text
trace_id
request_id
user_id
session_id
message_id
```

---

## 6.4 Metrics

至少提供：

```text
gateway_requests_total
gateway_attempts_total
gateway_errors_total
gateway_retries_total
gateway_fallbacks_total
gateway_cache_hits_total

gateway_total_duration_ms
gateway_upstream_duration_ms
gateway_ttft_ms
gateway_queue_duration_ms

gateway_input_tokens
gateway_output_tokens
gateway_total_tokens

gateway_total_cost
gateway_retry_waste_cost
gateway_hedge_waste_cost
```

维度：

```text
provider
hashed_channel_id
requested_model
resolved_model
error_category
status
```

---

## 6.5 若移出当前阶段

需要修改：

```text
Phase 3 Development Spec
OpenSpec Gateway Contract
Definition of Done
CLAUDE.md Phase Status
Release Notes
```

明确：

```text
Phase 3.0 只冻结 Runtime / Contract
Phase 3.2 负责 UI / Metrics
```

不得保持文档要求 UI/Metrics 已完成，却仅实现 Runtime。

---

# 7. P1-1：Streaming Duration 字段语义

## 7.1 当前问题

Streaming Wrapper 接受：

```text
duration_ms
connect_duration_ms
```

并在 Wrapper 创建时写入 Attempt。

若 `duration_ms` 表示“请求到响应头”：

```text
响应头耗时 = 300 ms
完整流消费 = 20 s
```

最终可能出现：

```text
Span duration = 20 s
gateway.upstream_duration_ms = 300 ms
```

字段语义不一致。

---

## 7.2 修复方案

建议拆分：

```text
gateway.upstream_connect_duration_ms
gateway.upstream_headers_duration_ms
gateway.upstream_total_duration_ms
```

若不拆分，则冻结：

```text
gateway.upstream_duration_ms = 完整上游流生命周期
```

Streaming 终态时覆盖为：

```python
upstream_total_duration_ms = (
    terminal_time - attempt_start_time
) * 1000
```

不得继续保留响应头时刻的 duration 作为总耗时。

---

## 7.3 测试

```text
test_stream_headers_duration_distinct_from_total_duration
test_stream_total_duration_covers_consumption
test_stream_cancel_total_duration_covers_partial_consumption
test_non_streaming_duration_semantics_unchanged
```

---

# 8. P1-2：Terminal Event 互斥组

## 8.1 当前问题

Recorder 当前只保证同名 Terminal Event 不重复。

但不同 Terminal Event 仍可能同时存在：

```text
attempt.completed + attempt.failed
response.completed + response.failed
stream.completed + stream.cancelled
```

---

## 8.2 修复要求

增加互斥组：

```python
_TERMINAL_GROUPS = {
    "attempt": {
        "gateway.attempt.completed",
        "gateway.attempt.failed",
    },
    "response": {
        "gateway.response.completed",
        "gateway.response.failed",
    },
    "stream": {
        "gateway.stream.completed",
        "gateway.stream.cancelled",
    },
}
```

Recorder 保存：

```text
group → recorded_event
```

同一组记录一个事件后，其余事件全部拒绝。

---

## 8.3 测试

```text
test_attempt_completed_then_failed_rejected
test_attempt_failed_then_completed_rejected
test_response_completed_then_failed_rejected
test_stream_completed_then_cancelled_rejected
```

---

# 9. Phase 3 分阶段冻结方案

推荐将阶段拆为：

```text
Phase 3.0 — Gateway Contract & Runtime
Phase 3.1 — One-API Production Integration
Phase 3.2 — Gateway UI & Metrics
```

## Phase 3.0 Definition of Done

```text
Router / Attempt Contract 冻结
Streaming Terminal 原子
Hedged Winner 语义冻结或明确不支持
Usage / Cost 聚合正确
Privacy / Sampling / Fail-open 正确
Context / Registry 无泄漏
Gateway Runtime E2E 通过
CI 全绿
```

## Phase 3.1 Definition of Done

```text
真实 One-API 生命周期接入
真实 Retry/Fallback/Streaming
真实 One-API E2E
不侵入 One-API 路由语义
```

## Phase 3.2 Definition of Done

```text
Router Detail
Attempt Timeline
Route Decision
Cost Breakdown
Gateway Filters
Gateway Metrics
Retry/Hedge Waste Cost
```

---

# 10. 推荐实施顺序

```text
Step 1  Streaming Terminal Atomic State Machine
Step 2  Recorder Terminal Event Mutual Exclusion
Step 3  Hedged Winner Contract
Step 4  Hedged / Parallel Runtime Implementation
Step 5  Streaming Duration Semantics
Step 6  决定 One-API：真实接入或拆分 Phase 3.1
Step 7  决定 UI/Metrics：实现或拆分 Phase 3.2
Step 8  更新 OpenSpec / Development Spec / CLAUDE.md
Step 9  全量测试与 CI
Step 10 Phase 3.0 冻结
```

---

# 11. 必须新增的测试文件

建议新增：

```text
sdk/tests/gateway_observability/
├── test_stream_terminal_atomicity.py
├── test_terminal_event_exclusion.py
├── test_hedged_attempt_winner.py
├── test_stream_duration_semantics.py
└── test_oneapi_production_integration.py
```

UI / Metrics：

```text
tests/
├── test_gateway_trace_summary.py
├── test_gateway_filters.py
├── test_gateway_metrics.py
└── test_retry_waste_cost.py
```

---

# 12. CI 要求

新增或强化：

```text
gateway-terminal-race-tests
gateway-hedged-attempt-tests
oneapi-production-e2e
gateway-ui-tests
gateway-metrics-tests
phase2-regression
```

并继续保证：

```text
Secret 缺失时受信任分支失败
Fork PR 不运行 Secret Job
Real E2E passed > 0
Real E2E skipped == 0
日志脱敏
```

---

# 13. 验收矩阵

| 能力 | 验收条件 |
|---|---|
| Streaming Terminal | 并发竞争下恰好一个终态 |
| Attempt Event | completed/failed 互斥 |
| Router Event | completed/failed 互斥 |
| Stream Event | completed/cancelled 互斥 |
| Hedged Winner | 最终状态由显式 Winner 决定 |
| Parallel Cost | 所有 Attempt Cost 均聚合 |
| Retry Waste | 失败 Retry Cost 可计算 |
| One-API | 真实生命周期驱动 Runtime |
| UI | Router Detail + Attempt Timeline |
| Metrics | 请求/尝试/错误/延迟/Token/Cost |
| Privacy | 原始 Channel/Secret 不进入 Telemetry |
| Sampling | 上游 sampled 决定不被覆盖 |
| Cleanup | Context/Registry/Stream 无残留 |
| CI | 全部 Job 成功且必需 E2E 0 skipped |

---

# 14. Definition of Done

本轮完成必须满足：

```text
1. Streaming Success/Error/Cancel 使用原子终态声明
2. 同一 Stream 只记录一个 Terminal Event
3. 同一 Attempt 只记录 completed 或 failed
4. 同一 Router 只记录 completed 或 failed
5. Router/Attempt 状态在所有并发终态下保持一致
6. Hedged/Parallel 的最终 Winner 语义冻结
7. Router final status/channel/error 由 Winner 决定
8. Usage/Cost 继续聚合全部 Attempt
9. Streaming Duration 字段语义一致
10. One-API 真实接入完成，或正式拆分为 Phase 3.1
11. Gateway UI/Metrics 完成，或正式拆分为 Phase 3.2
12. OpenSpec 与实现范围一致
13. 新增并发竞争测试全部通过
14. Phase 2.1～2.5 Regression 全部通过
15. GitHub CI 全部成功
16. 必需 E2E 无 Skip
```

---

# 15. 最终状态建议

在本轮修复完成前：

```text
Phase 3.0 — Gateway Contract & Runtime
⚠ MAIN IMPLEMENTATION COMPLETE
⚠ FINAL ACCEPTANCE GAPS

Phase 3.1 — One-API Production Integration
❌ PENDING

Phase 3.2 — Gateway UI & Metrics
❌ PENDING
```

完成本轮后：

```text
Phase 3.0 — Gateway Contract & Runtime
✅ COMPLETE
✅ FROZEN
```

只有 Phase 3.1 和 Phase 3.2 也完成时，才可以标记：

```text
Phase 3 — Gateway Native Observability
✅ COMPLETE
✅ FROZEN
```

---

# 16. 本轮禁止事项

```text
仅通过修改测试绕过并发终态问题
继续用普通布尔值做跨线程终态声明
用“最后完成 Attempt”隐式表示 Winner
只保留 OneApiAdapter 映射却宣称真实接入完成
只用通用 Trace Tree 代替 Gateway 专用 UI
文档仍写 UI/Metrics 完成但代码未实现
未完成验收就创建 v3.0-complete
```
