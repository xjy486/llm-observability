# LLM Observability Phase 3 返工问题需求报告

> 项目：`xjy486/llm-observability`  
> 审查提交：`6c00760b7329d9c4e71b9de818f89bb3122c8930`  
> 前置冻结提交：`63c148afa8fcbaefdb612a0af9f0c39cad0dba6e`  
> 当前阶段：Phase 3 — Gateway Native Observability  
> 审查结论：`CHANGES REQUIRED`  
> 当前建议定位：`Phase 3.0 — Gateway Contract / Runtime Prototype`  
> 文档目标：关闭 Trace、Context、Streaming、Usage、Privacy、Real E2E 等核心缺陷，使 Phase 3 达到可冻结标准。

---

# 1. 当前实现状态

当前提交已完成：

```text
Gateway Contract 基础
Router / Attempt GATEWAY 层级
GatewayRuntime
GatewayAdapter / GenericAdapter
One-API Adapter 骨架
LiteLLM Adapter 占位
Retry / Fallback / Cache / Rate-limit API
Streaming Wrapper
Usage / Cost 模型
PrivacyGuard
Sampling
Fail-open
Registry
Mock E2E
CI Job 骨架
```

当前主要问题不是“没有实现”，而是：

```text
实现范围较完整
但部分核心语义不正确
测试没有覆盖关键反例
真实集成链路尚未成立
归档和完成标记过早
```

本轮返工目标不是重写 Phase 3，而是修复以下问题：

```text
P0-1 无 SDK 场景 TraceID 非法
P0-2 上游 Trace 元数据标记错误
P0-3 Attempt 关闭错误清空 Router Context
P0-4 Streaming 终态未正确聚合到 Router
P0-5 Router Finalize 无法关闭遗留 Attempt
P0-6 Gateway Real E2E 假阳性
P0-7 LLM Usage 跨进程所有权设计不成立
P0-8 Fallback / Route / Attempt Event 泄露原始 Channel ID

P1-1 默认 Attempt Index 重复
P1-2 Cost Calculator 未使用模型
P1-3 Event Contract 未真正接入生命周期
P1-4 Association 字段写入不完整
P1-5 Span Attribute 未统一经过 PrivacyGuard
P1-6 Sampling 传播测试不足
P1-7 OpenSpec 已提前归档
```

---

# 2. P0-1：无 SDK 请求必须生成合法 TraceID

## 2.1 问题

无 SDK Context、无上游 `traceparent` 时，当前 Router 可能创建：

```text
trace_id = None
parent_span_id = None
```

随后 Attempt 继承同一个空 TraceID。

这会导致：

```text
无法作为合法 W3C Trace
无法稳定入库
无法按 TraceID 查询
无法在 UI 中构建树
不同 Root 请求可能混入同一空键
```

## 2.2 修复要求

Router Parent Resolver 必须返回明确的来源类型：

```python
@dataclass(frozen=True)
class ResolvedGatewayParent:
    trace_id: str
    parent_span_id: Optional[str]
    origin: str
    upstream_trace_present: bool
```

来源：

```text
sdk_context
remote_traceparent
gateway_root
```

无 SDK、无远端 Trace 时：

```python
trace_id = generate_trace_id()
parent_span_id = None
origin = "gateway"
upstream_trace_present = False
```

必须满足：

```text
TraceID 长度 32
仅十六进制字符
全 0 TraceID 禁止
连续请求生成不同 TraceID
Attempt 继承 Router TraceID
```

## 2.3 测试

```text
test_no_sdk_router_generates_valid_trace_id
test_no_sdk_attempt_inherits_router_trace_id
test_no_sdk_requests_generate_distinct_trace_ids
test_router_never_reports_null_trace_id
test_router_never_reports_all_zero_trace_id
```

---

# 3. P0-2：正确区分 SDK、远端 Trace 和本地 Root

## 3.1 问题

当前有合法上游 `traceparent` 时，Router 能继承：

```text
trace_id
parent_span_id
```

但属性仍可能写成：

```text
gateway.trace_origin = gateway
gateway.upstream_trace_present = false
```

这与实际父子链矛盾。

## 3.2 修复要求

属性语义冻结：

### SDK 当前进程 Context

```text
gateway.trace_origin = sdk
gateway.upstream_trace_present = true
```

### 远端 traceparent

```text
gateway.trace_origin = remote
gateway.upstream_trace_present = true
```

### 无上游本地 Root

```text
gateway.trace_origin = gateway
gateway.upstream_trace_present = false
```

禁止通过“是否存在 Parent 对象”间接推断来源。

## 3.3 测试

```text
test_sdk_context_sets_trace_origin_sdk
test_remote_traceparent_sets_trace_origin_remote
test_remote_traceparent_sets_upstream_trace_present_true
test_local_root_sets_trace_origin_gateway
test_trace_metadata_consistent_with_parent_ids
```

---

# 4. P0-3：Attempt 结束后必须保留 Router Context

## 4.1 问题

正常流程：

```text
Router start
Attempt start
Attempt close
```

当前 Attempt 清理后可能将整个 Gateway Context 清空，导致：

```text
runtime.active_router() == None
```

影响：

```text
后续 Retry
Fallback
第二次 Attempt
Router Event
Usage 聚合
Cost 聚合
本地 LLM Usage Hook
```

## 4.2 修复要求

Context 应分别管理 Router 和 Attempt。

推荐：

```python
@dataclass(frozen=True)
class GatewayContextState:
    router: Optional[RouterSpan]
    active_attempt: Optional[AttemptSpan]
```

Attempt 清理只允许：

```text
active_attempt → previous_attempt / None
router → 保持不变
```

不得在 Attempt 正常关闭时调用：

```python
clear_gateway_context()
```

只有 Router 终态才允许清理 Router Context。

Cross-context Reset 时：

```text
只清理当前 Attempt Slot
不得顺带清理 Router Slot
```

## 4.3 测试

```text
test_attempt_close_preserves_active_router
test_attempt_error_preserves_active_router
test_retry_second_attempt_uses_same_router
test_fallback_second_attempt_uses_same_router
test_attempt_close_clears_only_attempt_slot
test_router_close_clears_router_and_attempt_slots
test_async_attempt_close_preserves_router
test_cross_context_attempt_reset_preserves_router
```

---

# 5. P0-4：重构 Streaming 最终状态与 Router 聚合

## 5.1 问题

当前 Streaming 可能先调用：

```text
finish_attempt(status=200)
```

将 Attempt 作为成功聚合到 Router，然后在流消费期间发生：

```text
client_cancelled
stream_interrupted
timeout
```

Streaming Wrapper 只修改 Attempt Span，但不重新构造并聚合最终 `AttemptResult`。

结果可能是：

```text
Router = OK
Attempt = ERROR client_cancelled
```

这违反父子状态语义。

## 5.2 修复要求

禁止 Streaming 在收到 Header 或创建流对象时完成 Attempt。

明确拆分：

```python
finish_non_streaming_attempt(...)
finalize_streaming_attempt(...)
```

Streaming 生命周期：

```text
Attempt.start
收到响应头
返回 Streaming Wrapper
读取首个有效内容
正常完成 / 失败 / 取消
构造最终 AttemptResult
聚合 Router
关闭 Attempt
关闭 Router
```

### Streaming Success

```text
Attempt status = OK
Router status = OK
AttemptResult.success = true
```

### Streaming Error

```text
Attempt status = ERROR
Attempt error_category = stream_interrupted / timeout / connect_error
Router status = ERROR
Router final_error_category = Attempt error_category
```

### Client Cancel

```text
Attempt status = ERROR 或 CANCELLED 语义映射
Attempt error_category = client_cancelled
Router status = ERROR
Router final_error_category = client_cancelled
```

如果产品决定客户端主动取消不算 ERROR，也必须冻结统一状态语义，Router 与 Attempt 必须一致，不得一个 OK、一个 ERROR。

## 5.3 TTFT

TTFT 应从：

```text
真实上游请求开始时间
```

计算，而不是 Wrapper 创建时间。

`first_token` 必须定义为：

```text
第一个有效模型内容
```

以下不应触发 TTFT：

```text
SSE keepalive
空字符串
仅 metadata chunk
usage-only chunk
[DONE]
```

## 5.4 Streaming Usage

最终 Chunk 中存在 Usage 时：

```text
Attempt Usage
→ Router Aggregate
```

取消或失败时若上游已经返回部分 Usage，也要按 Provider 能力记录。

## 5.5 测试

```text
test_stream_success_aggregates_attempt_once
test_stream_cancel_router_and_attempt_both_error
test_stream_error_router_and_attempt_both_error
test_stream_timeout_router_final_error_timeout
test_stream_does_not_aggregate_success_before_terminal_state
test_stream_attempt_result_registered_once
test_stream_usage_aggregated_at_terminal_chunk
test_stream_ttft_ignores_keepalive
test_stream_ttft_ignores_done_marker
test_stream_close_is_idempotent
test_async_stream_cancelled_error_finalizes_once
test_async_stream_aclose_finalizes_once
```

---

# 6. P0-5：Router Finalize 必须关闭所有遗留 Attempt

## 6.1 问题

当前 `GatewayRuntimeHandle.finalize()` 使用 Router SpanID 删除 Attempt Registry，无法匹配真正的 Attempt Entry。

异常窗口：

```text
Attempt.start()
业务抛异常
Attempt.close() 未执行
Router.finalize()
```

可能遗留：

```text
未结束 Attempt
Attempt Registry Entry
active_attempt Context
Reporter 未上报终态
```

## 6.2 修复要求

Router 必须显式维护 Open Attempt：

```python
self._open_attempts: dict[str, AttemptSpan]
```

Attempt：

```text
start → register_open_attempt
close → unregister_open_attempt
```

Router Finalize：

```python
for attempt in snapshot(open_attempts):
    attempt.force_close(
        category="gateway_internal",
        reason="router_finalized_with_open_attempt",
    )
```

禁止只删除 Registry 而不结束 Span。

`force_close()` 必须：

```text
幂等
Fail-open
不覆盖已有业务错误
清理 Registry
清理 Context
上报最终 Span
```

## 6.3 测试

```text
test_router_finalize_force_closes_open_attempt
test_router_finalize_multiple_open_attempts
test_exception_between_attempt_start_and_close_no_leak
test_force_close_is_idempotent
test_router_finalize_after_attempt_end_does_not_duplicate_report
test_open_attempt_registry_empty_after_router_finalize
```

---

# 7. P0-6：实现真正的 Gateway Real E2E

## 7.1 问题

当前 `gateway-real-e2e` 实际运行的是 Phase 2.5 Real E2E 文件，并使用不匹配的环境变量：

```text
Job:
GATEWAY_E2E_API_KEY
GATEWAY_E2E_BASE_URL

Test:
E2E_API_KEY
E2E_BASE_URL
E2E_MODEL
```

因此可能：

```text
全部测试 Skip
CI 返回成功
```

当前日志脱敏：

```bash
sed 's/$GATEWAY_E2E_API_KEY/<redacted>/g'
```

由于单引号，变量不会展开，无法脱敏真实 Secret。

## 7.2 修复要求

新增：

```text
sdk/tests/gateway_observability/test_real_gateway_e2e.py
```

真实链路至少包含：

```text
Client
→ Gateway Middleware / Adapter
→ GatewayRuntime
→ Router
→ Attempt
→ Mock 或真实 Upstream
→ Reporter
→ Mock Core Ingest
```

不能只直接调用 Runtime 并检查内存对象。

### 必须覆盖

```text
一次成功
一次 Retry
一次 Fallback
Streaming Success
Streaming Cancel
无 SDK Trace
上游 sampled=0
隐私
```

### 硬断言

```text
Router/Attempt 实际进入 Core
TraceID 合法
Attempt.parent = Router
Router.parent = SDK LLM 或 Remote Parent
Retry 产生多个唯一 Attempt
Fallback from/to 正确且已哈希
Streaming 终态一致
Registry/Context 最终为空
```

## 7.3 CI

统一变量：

```text
GATEWAY_E2E_API_KEY
GATEWAY_E2E_BASE_URL
GATEWAY_E2E_MODEL
```

测试必须读取同名变量。

可信分支：

```text
Secret 缺失 → CI 失败
```

Fork PR：

```text
整个 Secret Job 不执行
```

日志脱敏推荐：

```bash
python -m pytest ... 2>&1 \
  | python scripts/redact_ci_secrets.py
```

不要使用直接把 Secret 插入 `sed` 表达式的方式。

新增 CI 检查：

```text
运行测试数 > 0
skipped 必需 E2E 数量 = 0
```

---

# 8. P0-7：重新冻结 LLM / Router / Attempt Usage 所有权

## 8.1 问题

当前通过 Gateway `ContextVar` 将 Router Aggregate 回写到 SDK LLM Span。

这只在：

```text
SDK 和 Gateway Runtime 位于同一 Python 进程
```

时成立。

真实部署通常是：

```text
应用进程：LLM Span
网关进程：Router / Attempt Span
```

ContextVar 不能跨进程共享。

## 8.2 推荐所有权

### Attempt

```text
每一次真实 Provider 请求
本次 Usage
本次 Cost
```

### Router

```text
所有 Attempt 的实际 Usage / Cost 汇总
包含失败与重试
```

### SDK LLM

```text
调用方收到的逻辑响应 Usage
不强制等于 Router Aggregate
```

### Core / UI

通过 Trace 树计算：

```text
Logical Usage = LLM Usage
Actual Gateway Usage = Router Usage
Retry Waste = Router Usage - Final Successful Attempt Usage
```

这样可以同时展示：

```text
用户逻辑消耗
网关实际计费消耗
重试浪费
```

## 8.3 允许的可选协议

若必须把 Router Aggregate 回传客户端，可设计显式协议：

```text
Response Header
HTTP Trailer
SSE Final Metadata
```

例如：

```text
x-llm-obs-input-tokens
x-llm-obs-output-tokens
x-llm-obs-total-cost
```

但必须单独设计：

```text
签名
大小限制
信任边界
代理兼容
Streaming 支持
```

本轮推荐删除“LLM 必须等于 Router Aggregate”的强制本地 ContextVar 语义。

## 8.4 测试

```text
test_router_usage_is_sum_of_all_attempts
test_llm_usage_remains_logical_response_usage
test_failed_retry_usage_counted_in_router
test_retry_waste_can_be_derived
test_cross_process_trace_does_not_require_shared_contextvar
```

---

# 9. P0-8：Channel ID 必须全链路 Hash

## 9.1 问题

Span 中 Channel ID 会 Hash，但 Event 中可能记录原始值：

```text
gateway.route.selected
gateway.attempt.started
gateway.fallback.selected
```

Fallback 还只记录了 `to_channel`，缺少 `from_channel`。

## 9.2 修复要求

冻结 Event 字段：

```text
gateway.fallback.selected:
  from_channel_id
  to_channel_id
  reason
```

但两者写入前必须调用同一个：

```python
PrivacyGuard.hash_channel_id()
```

建议属性名直接体现已匿名化：

```text
from_channel_id_hash
to_channel_id_hash
channel_id_hash
```

或者保持 Contract 原名，但所有来源统一保证值为 Hash。

以下路径必须一致：

```text
Router Attribute
Attempt Attribute
Route Event
Attempt Event
Fallback Event
日志
Metrics
```

禁止任何 Telemetry 中出现原始 Channel ID。

## 9.3 测试

```text
test_route_event_channel_id_is_hashed
test_attempt_event_channel_id_is_hashed
test_fallback_event_contains_from_and_to
test_fallback_event_from_and_to_are_hashed
test_raw_channel_id_absent_from_all_span_events_logs
test_same_channel_id_hash_is_stable
test_different_channel_ids_hash_differ
```

---

# 10. P1-1：默认 Attempt Index 必须递增

## 10.1 问题

未提供 `attempt_index` 时，Runtime 可能总是使用：

```text
1
```

连续两个 Attempt：

```text
Attempt 1
Attempt 1
```

Router Count 仍可能显示 `1`。

## 10.2 修复要求

默认 Index 由 Router 分配：

```python
next_index = router.allocate_attempt_index()
```

规则：

```text
未显式提供 → 使用 next_index
显式提供合法正整数 → 使用显式值
显式值重复 → 拒绝、重映射或记录错误，必须冻结
0 / 负数 / 非整数 → 回退自动分配
```

推荐：

```text
重复显式 Index → 自动分配下一个可用值，并记录 warning
```

## 10.3 测试

```text
test_default_attempt_index_increments
test_attempt_count_matches_actual_attempts
test_duplicate_explicit_attempt_index_handled
test_invalid_attempt_index_falls_back
test_parallel_attempt_index_is_thread_safe
```

---

# 11. P1-2：Cost Calculator 必须使用 Resolved Model

## 11.1 问题

`CostCalculator.calculate()` 支持 `model`，但 Runtime 没有传入：

```text
attempt.resolved_model
```

Pricing Table 无法命中。

`handle_cache(..., cost=...)` 接收 `cost` 参数但未使用。

## 11.2 修复要求

Attempt Cost：

```python
calculate(
    usage=normalized,
    model=attempt.resolved_model,
)
```

Cache：

```text
调用方明确传 cost → 使用调用方 cost
未传 cost、有 usage → 由 resolved model 计算
无价格 → cost.source=unpriced
```

价格表单位必须明确：

```text
USD / token
或
USD / 1M tokens
```

禁止含糊。

推荐使用：

```text
USD per 1M tokens
```

并在配置中显式命名：

```text
input_usd_per_1m_tokens
output_usd_per_1m_tokens
```

## 11.3 测试

```text
test_cost_uses_resolved_model
test_cost_unknown_model_is_unpriced
test_cache_explicit_cost_is_preserved
test_retry_cost_sums_all_attempts
test_failed_attempt_cost_included
test_pricing_unit_is_per_1m_tokens
```

---

# 12. P1-3：Event Contract 必须真正接入生命周期

## 12.1 问题

以下方法虽然存在，但没有稳定调用：

```text
route_selected
attempt_started
attempt_completed
attempt_failed
response_completed
response_failed
```

当前属于“声明了 Contract，但 Runtime 未真正使用”。

## 12.2 修复要求

### Router Start

```text
gateway.auth.started / completed
gateway.route.started / selected
```

首版若没有具体 Auth Hook，可不伪造 Auth Event。

### Attempt Start

```text
gateway.attempt.started
```

### Attempt Success

```text
gateway.attempt.completed
```

### Attempt Failure

```text
gateway.attempt.failed
```

### Router Final

```text
gateway.response.completed
gateway.response.failed
```

每个终态 Event 必须：

```text
最多一次
在 Span.end 前写入
属性经过 PrivacyGuard
```

## 12.3 测试

```text
test_attempt_start_event_exactly_once
test_attempt_completed_event_exactly_once
test_attempt_failed_event_exactly_once
test_router_response_completed_exactly_once
test_router_response_failed_exactly_once
test_no_success_and_failed_events_on_same_attempt
```

---

# 13. P1-4：Association 字段完整写入

## 13.1 问题

Request Context 包含：

```text
user_id
session_id
message_id
app_name
business_scenario
```

Router 当前仅稳定写入部分字段。

## 13.2 修复要求

统一使用 Phase 2.5 Association Resolver。

Router 必须写入：

```text
user_id
session_id
message_id
app_name
business_scene / business_scenario
```

命名必须与现有 Span Record 保持一致，不要同时出现：

```text
business_scene
business_scenario
```

两个不兼容字段。

优先级：

```text
Gateway Request 显式值
> Remote Association Header / Baggage
> None
```

## 13.3 测试

```text
test_router_all_association_fields
test_attempt_does_not_duplicate_sensitive_association
test_remote_association_propagates_to_router
test_local_gateway_association_overrides_remote
test_association_values_are_sanitized
```

---

# 14. P1-5：所有 Span Attribute 统一经过 PrivacyGuard

## 14.1 问题

目前 Channel ID 经过 Hash，Event 经过 Sanitization，但大量 Span Attribute 直接写入：

```text
route
route_reason
policy_name
request_id
provider
resolved_model
upstream_request_id
error_message
```

## 14.2 修复要求

新增统一入口：

```python
def set_gateway_attribute(span, key, value, privacy_guard) -> bool:
    ...
```

流程：

```text
字段名白名单
Value Sanitization
长度限制
类型规范化
Size Guard
span.set_attribute
Fail-open
```

Router 和 Attempt 禁止直接写不受保护的外部字符串。

建议限制：

```text
单字符串最大 512 bytes
request_id 最大 256 bytes
route 最大 256 bytes
reason 最大 256 bytes
provider/model 最大 128 bytes
```

## 14.3 测试

```text
test_router_external_values_sanitized
test_attempt_external_values_sanitized
test_request_id_size_limited
test_route_query_removed
test_error_message_secret_redacted
test_span_attributes_default_deny_unknown_keys
```

---

# 15. P1-6：Sampling 传播必须覆盖 Downstream Header

## 15.1 问题

当前测试主要检查：

```text
Router TraceID 继承
Reporter 不上报
```

但“继续传播 traceparent”并未真正验证 Header。

## 15.2 修复要求

Gateway Runtime 或 Adapter 应提供：

```python
inject_downstream_trace_headers(router, attempt)
```

Attempt 下游 Header：

```text
trace_id = Router.trace_id
parent_span_id = Attempt.span_id
trace_flags = inherited sampled flag
```

采样为 0 时：

```text
trace_flags=00
```

## 15.3 测试

```text
test_attempt_downstream_traceparent_parent_is_attempt
test_sampled_zero_downstream_trace_flags_00
test_sampled_one_downstream_trace_flags_01
test_remote_trace_id_preserved_downstream
test_local_root_trace_id_propagated_downstream
```

---

# 16. P1-7：OpenSpec Change 不应提前归档

## 16.1 问题

当前 Change 已放入：

```text
openspec/changes/archive/
```

任务全部标记完成，但仍存在多个 P0。

## 16.2 修复要求

创建新的返工 Change：

```text
fix-gateway-native-observability-closeout
```

不要修改已归档历史来伪装首次实现正确。

新 Change 应包含：

```text
proposal.md
design.md
tasks.md
spec deltas
```

完成所有返工、CI、Real E2E 和审查后再归档。

---

# 17. 推荐实施顺序

```text
Step 1  修复 Root TraceID 与 Trace Origin
Step 2  修复 Gateway Context 分槽语义
Step 3  修复 Attempt Index 与 Open Attempt Registry
Step 4  重构 Streaming Terminal Finalization
Step 5  修复 Router / Attempt 最终状态一致性
Step 6  修复 Event 生命周期接入
Step 7  修复 Channel ID Hash 与 Attribute Privacy
Step 8  重新冻结 Usage / Cost 所有权
Step 9  补齐 Downstream Trace Propagation
Step 10 完成真实 Gateway HTTP E2E
Step 11 修复 CI Secret / Skip / Redaction
Step 12 Phase 2.1～2.5 回归
Step 13 重新审查
Step 14 归档返工 Change
```

---

# 18. 建议修改文件

核心：

```text
sdk/python/llm_observability/gateway_observability/context.py
sdk/python/llm_observability/gateway_observability/router_span.py
sdk/python/llm_observability/gateway_observability/attempt_span.py
sdk/python/llm_observability/gateway_observability/runtime.py
sdk/python/llm_observability/gateway_observability/streaming.py
sdk/python/llm_observability/gateway_observability/recorder.py
sdk/python/llm_observability/gateway_observability/registry.py
sdk/python/llm_observability/gateway_observability/privacy.py
sdk/python/llm_observability/gateway_observability/propagation.py
sdk/python/llm_observability/gateway_observability/aggregation.py
sdk/python/llm_observability/gateway_observability/cost.py
sdk/python/llm_observability/instrumentation/openai.py
sdk/python/llm_observability/integrations/langchain/llm_span.py
sdk/python/llm_observability/integrations/oneapi/adapter.py
.github/workflows/ci.yml
```

建议新增：

```text
sdk/tests/gateway_observability/test_trace_identity.py
sdk/tests/gateway_observability/test_context_lifecycle.py
sdk/tests/gateway_observability/test_stream_terminal_state.py
sdk/tests/gateway_observability/test_open_attempt_cleanup.py
sdk/tests/gateway_observability/test_channel_privacy.py
sdk/tests/gateway_observability/test_usage_ownership.py
sdk/tests/gateway_observability/test_downstream_propagation.py
sdk/tests/gateway_observability/test_real_gateway_e2e.py
scripts/redact_ci_secrets.py
openspec/changes/fix-gateway-native-observability-closeout/
```

---

# 19. 验收矩阵

| 能力 | 必须满足 |
|---|---|
| Root Trace | 无 SDK 时生成合法且唯一 TraceID |
| Remote Trace | 正确继承 TraceID、ParentID、Sampling |
| Trace Origin | sdk / remote / gateway 三种来源准确 |
| Attempt Context | Attempt 结束不清除 Router |
| Router Context | 仅 Router 终态清除 |
| Attempt Index | 默认递增且并发安全 |
| Open Attempt | Router Finalize 强制关闭遗留 Attempt |
| Streaming Success | Router/Attempt 均为成功 |
| Streaming Error | Router/Attempt 错误一致 |
| Streaming Cancel | Router/Attempt 取消语义一致 |
| TTFT | 从真实请求开始到首个有效内容 |
| Retry | 每次请求唯一 Attempt |
| Fallback | from/to 均存在且已哈希 |
| Usage | Router 汇总所有 Attempt |
| LLM Usage | 不依赖跨进程 ContextVar 回写 |
| Cost | 使用 resolved_model，包含失败重试 |
| Privacy | 原始 Channel ID 永不进入 Telemetry |
| Events | Start/Complete/Failed 生命周期事件准确且唯一 |
| Propagation | Attempt 下游 traceparent 正确 |
| Sampling=0 | 业务正常、Header=00、不上报 |
| Real E2E | 真实 HTTP Gateway Runtime → Core 链路 |
| CI | 必需 E2E 不允许全部 Skip |
| Regression | Phase 2.1～2.5 全部通过 |

---

# 20. Definition of Done

只有全部满足以下条件，返工才算完成：

```text
1. 无 SDK Root Router 使用合法 32 位 TraceID
2. Attempt 继承 Router TraceID
3. SDK / Remote / Gateway Root 来源字段准确
4. Attempt 关闭后 Router Context 仍活跃
5. Router 结束后所有 Gateway Context 清空
6. Streaming 不在响应头阶段聚合成功
7. Streaming Success/Error/Cancel 正确聚合到 Router
8. Router 与 Attempt 最终状态一致
9. Router Finalize 会关闭所有 Open Attempt
10. Registry 在所有终态回到 0
11. Attempt Index 默认递增且并发安全
12. Fallback Event 包含 from/to
13. 所有 Channel ID 在 Span/Event 中均为 Hash
14. Usage 所有权支持真实跨进程部署
15. Router Usage 包含失败重试 Usage
16. Cost 使用 resolved_model
17. Downstream traceparent 由 Attempt 继续传播
18. Sampling=0 保持 trace_flags=00
19. 真正的 Gateway HTTP E2E 进入 Core
20. Gateway Real E2E 不复用 Phase 2.5 测试文件
21. CI Secret 名称与测试读取一致
22. CI 必需验收测试为 0 skipped
23. Phase 2.1～2.5 全量回归通过
24. GitHub CI 全部成功
25. 返工 OpenSpec Change 经审查后归档
```

完成后才能标记：

```text
Phase 3 — Gateway Native Observability
✅ COMPLETE
✅ FROZEN
```

---

# 21. 本轮禁止事项

```text
通过放宽测试断言规避状态错误
只清 Registry 不结束 Span
在 Attempt Close 中清空整个 Gateway Context
在 Streaming Header 阶段记录 Attempt 成功
继续用 ContextVar 假设跨进程 Usage 回填
在 Event 中记录原始 Channel ID
让 gateway-real-e2e 继续运行 Phase 2.5 测试
允许必需 E2E 全部 Skip 后 CI 绿色
用单引号 sed 假装脱敏 Secret
修改 One-API 路由行为
新增新的 SpanKind
吞掉业务异常
未完成 Real E2E 就再次归档
```

---

# 22. 质量改进要求

本次返工除了修 Bug，还应调整开发方式。

## 22.1 Change 不再一次性全部实现并归档

建议拆为三个检查点：

```text
Checkpoint 1：
Trace + Context + Attempt Lifecycle

Checkpoint 2：
Streaming + Usage/Cost + Privacy

Checkpoint 3：
Real E2E + CI + Regression
```

每个检查点完成后先审查，再进入下一部分。

## 22.2 测试采用对抗性断言

测试不仅检查：

```text
某属性存在
某对象已结束
```

还必须检查：

```text
父子状态是否矛盾
TraceID 是否合法
是否发生重复聚合
是否存在真实入库记录
是否有 Secret 泄漏
是否有 Registry 残留
是否有必需测试被 Skip
```

## 22.3 完成标记必须有三层证据

```text
代码审查通过
GitHub CI 可见且成功
Real E2E 真实执行且 0 skipped
```

三者缺一，不得标记 `COMPLETE / FROZEN`。
