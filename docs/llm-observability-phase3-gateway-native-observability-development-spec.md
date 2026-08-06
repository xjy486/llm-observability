# LLM Observability Phase 3 开发需求文档

> 项目：`xjy486/llm-observability`  
> 阶段：Phase 3 — Gateway Native Observability  
> 前置基线：Phase 2.5 已完成并冻结  
> 冻结提交：`63c148afa8fcbaefdb612a0af9f0c39cad0dba6e`  
> 建议 OpenSpec Change：`add-gateway-native-observability-contract`  
> 文档目标：冻结网关原生观测的 Trace 语义、Runtime 接口、Adapter 边界、重试/降级/流式生命周期、Usage/Cost 所有权与真实 E2E 验收标准。

---

# 1. 背景

Phase 2.5 已完成 SDK 侧能力闭环：

```text
AGENT
├── TASK
├── TOOL
└── LLM
    └── GATEWAY
```

目前的 GATEWAY 主要代表“模型请求经过网关”这一事实，但尚不能完整解释网关内部发生了什么：

```text
请求进入了哪个网关
鉴权是否通过
命中了什么租户、应用和策略
请求模型被映射成什么模型
选择了哪个渠道
为什么选择该渠道
是否命中缓存
是否被限流或排队
是否发生上游重试
是否切换备用渠道
每次上游请求的延迟、Token 和费用是多少
流式响应何时开始、何时结束、为何中断
```

Phase 3 的目标是将这些网关内部决策接入现有 Trace，使用户能够从一条 Trace 中回答：

```text
一次逻辑模型调用最终去了哪里
为什么这样路由
中间失败了几次
哪个渠道最慢
哪个渠道最贵
哪里发生了降级
最终 Usage 和 Cost 如何构成
```

---

# 2. 阶段目标

Phase 3 建立一套与具体网关实现解耦的 **Gateway Native Observability Contract**。

必须实现：

```text
1. Router GATEWAY Span
2. Provider Attempt GATEWAY Span
3. Route / Retry / Fallback / Cache / Rate-limit Event
4. Streaming 完整生命周期
5. Usage / Cost 统一归一化
6. One-API 胶水层 Adapter
7. LiteLLM 可替换接口
8. SDK → Gateway Trace 关联与去重
9. 无 SDK 调用时的独立网关 Trace
10. 真实 Gateway E2E
```

最终目标链路：

```text
AGENT
└── LLM
    └── GATEWAY router
        ├── GATEWAY attempt-1
        └── GATEWAY attempt-2
```

或无 SDK 场景：

```text
GATEWAY router
└── GATEWAY attempt-1
```

## 2.1 阶段拆分（Phase 3.0 / 3.1 / 3.2）

Phase 3 正式拆分为三个可独立冻结的子阶段。本文档第 28 节 Definition of Done
仅约束 **Phase 3.0**；One-API 真实生命周期接入与 Gateway 专用 UI/Metrics 不在
Phase 3.0 DoD 内，分别由独立 change 承接。

```text
Phase 3.0 — Gateway Contract & Runtime
  Router/Attempt Contract 冻结
  Streaming Terminal 原子状态机（first terminal claim wins）
  Hedged/Parallel Winner 语义（显式 select_winner + 确定性 fail-safe）
  Terminal Event 互斥组
  Streaming Duration 字段语义
  Usage/Cost 聚合（含失败/loser Attempt）
  Privacy / Sampling / Fail-open
  Context/Registry 无泄漏
  Gateway Runtime E2E 通过、CI 全绿、必需 E2E 0 skipped
  → 本轮冻结

Phase 3.1 — One-API Production Integration（future change）
  真实 One-API 生命周期接入（middleware/hooks/lifecycle/streaming/bootstrap）
  真实 Retry/Fallback/Streaming 由 One-API 事件驱动 Runtime
  真实/容器化 One-API E2E
  不侵入 One-API 路由语义、Telemetry fail-open

Phase 3.2 — Gateway UI & Metrics（future change）
  Router Detail / Attempt Timeline / Route Decision / Cost Breakdown
  Gateway Filters（provider/channel/model/error_category/span_role/cache_status）
  Gateway Metrics（requests/attempts/errors/retries/fallbacks/cache/latency/tokens/cost）
  Retry/Hedge Waste Cost
```

只有 Phase 3.0、3.1、3.2 全部完成时，方可标记 `Phase 3 — Gateway Native
Observability ✅ COMPLETE/FROZEN`。本文档不创建 `oneapi-production-integration`
或 `gateway-ui-metrics` 的空白 spec 能力；3.1/3.2 由各自 change 在启动时建立
实质 requirement。

> 注：第 19–23 节（One-API Adapter 映射、UI 需求、Core/存储需求、指标需求、真实
> E2E）描述的是 Phase 3 的完整终态愿景。其中 One-API Adapter 字段映射
> （`OneApiAdapter`）已作为 Phase 3.0 的一部分实现并冻结；真实 One-API 生命周期
> 接入属 Phase 3.1，Gateway 专用 UI 与 Metrics 属 Phase 3.2。不得在文档中表述
> 3.1/3.2 已完成而代码未实现。

---

# 3. 非目标

本阶段不实现：

```text
修改 One-API 核心路由算法
自动控制渠道选择
自动执行降级策略
替代 One-API / LiteLLM
Prompt 管理平台
模型评测平台
MCP 原生观测
Claude Code / Codex 本地文件、Shell、审批事件
LangGraph Checkpoint / Interrupt 观测
Embedding / Rerank / Vector DB 完整体系
新增 SpanKind
完整保存原始 Prompt / Response
```

Phase 3 只负责：

```text
采集
关联
规范化
展示
诊断
```

---

# 4. 核心设计决策

## 4.1 SpanKind 不扩展

继续只使用：

```text
AGENT
TASK
TOOL
LLM
GATEWAY
```

不新增：

```text
ROUTER
PROVIDER
CHANNEL
RETRY
```

通过属性区分 GATEWAY 角色：

```text
gateway.span_role = router
gateway.span_role = provider_attempt
```

## 4.2 Router 与 Attempt 的父子关系

标准结构：

```text
LLM
└── GATEWAY router
    ├── GATEWAY attempt-1
    ├── GATEWAY attempt-2
    └── GATEWAY attempt-3
```

规则：

```text
Router.parent_span_id = SDK LLM.span_id
Attempt.parent_span_id = Router.span_id
所有 Span 使用同一个 TraceID
每次真实上游请求对应一个 Attempt Span
```

禁止：

```text
多个 Attempt 共用同一个 Span
重试只覆盖前一次 Attempt 数据
Attempt 直接挂在 LLM 下绕过 Router
```

## 4.3 SDK LLM 与 Gateway 所有权

SDK LLM 表示一次逻辑模型调用；Router GATEWAY 表示网关对这次逻辑调用的处理；Attempt GATEWAY 表示一次真实上游 Provider 请求。

```text
LLM
→ 逻辑输入、逻辑输出、最终汇总 Usage

Router GATEWAY
→ 网关总耗时、路由结果、最终渠道、重试/降级汇总

Attempt GATEWAY
→ 每次上游请求的请求结果、延迟、Token、费用、错误
```

## 4.4 无 SDK 调用时

直接请求网关时，网关必须仍产生 Trace：

```text
GATEWAY router
└── GATEWAY attempt-1
```

要求：

```text
Router 作为 Root Span
不额外伪造 LLM Span
不伪造 AGENT Span
```

属性：

```text
gateway.upstream_trace_present = false
gateway.trace_origin = gateway
```

## 4.5 Retry 与 Fallback

每一次真实请求都创建新的 Attempt Span：

```text
Router
├── Attempt 1：channel-a，HTTP 500
├── Attempt 2：channel-a，timeout
└── Attempt 3：channel-b，success
```

Router 使用 Event 记录决策过程：

```text
gateway.retry.scheduled
gateway.fallback.selected
```

Attempt 不复用。

## 4.6 Streaming 生命周期

Router 和 Attempt Span 必须持续到以下任一终态：

```text
正常消费完成
上游发送 [DONE]
客户端主动断开
客户端取消
上游超时
上游连接异常
Generator close
Async Generator aclose
```

不得在以下时机提前结束：

```text
收到响应头
建立上游连接
读取到第一个 Token
返回 StreamingResponse 对象
```

---

# 5. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│ Application / Agent / LangChain                     │
│                                                     │
│ AGENT → TASK/TOOL → LLM                             │
└───────────────────────┬─────────────────────────────┘
                        │ traceparent + baggage
                        ▼
┌─────────────────────────────────────────────────────┐
│ Gateway Adapter                                     │
│                                                     │
│ OneApiAdapter / LiteLLMAdapter / GenericAdapter     │
└───────────────────────┬─────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────┐
│ Gateway Observability Runtime                       │
│                                                     │
│ RouterSpan                                          │
│ AttemptSpan                                         │
│ GatewayEventRecorder                                │
│ UsageNormalizer                                     │
│ CostCalculator                                      │
│ PrivacyGuard                                        │
└───────────────────────┬─────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────┐
│ Core Ingest / Storage / UI                          │
└─────────────────────────────────────────────────────┘
```

---

# 6. 推荐目录结构

建议新增：

```text
gateway_observability/
├── __init__.py
├── context.py
├── runtime.py
├── router_span.py
├── attempt_span.py
├── adapter.py
├── attributes.py
├── events.py
├── usage.py
├── cost.py
├── errors.py
├── privacy.py
├── streaming.py
└── propagation.py
```

One-API 胶水层：

```text
integrations/oneapi/
├── __init__.py
├── adapter.py
├── request_mapper.py
├── response_mapper.py
├── channel_mapper.py
├── retry_mapper.py
└── usage_mapper.py
```

LiteLLM 预留：

```text
integrations/litellm/
└── adapter.py
```

---

# 7. Gateway Adapter 接口

## 7.1 抽象接口

```python
from abc import ABC, abstractmethod
from typing import Any


class GatewayAdapter(ABC):
    @abstractmethod
    def extract_request_context(self, request: Any) -> "GatewayRequestContext":
        ...

    @abstractmethod
    def extract_route_decision(self, internal_state: Any) -> "RouteDecision":
        ...

    @abstractmethod
    def extract_attempt_context(self, internal_state: Any) -> "AttemptContext":
        ...

    @abstractmethod
    def extract_usage(self, response: Any) -> "NormalizedUsage":
        ...

    @abstractmethod
    def classify_error(self, error: BaseException) -> "GatewayError":
        ...
```

## 7.2 Adapter 约束

Adapter 只负责：

```text
字段提取
概念映射
事件调用
状态归一化
```

Adapter 不负责：

```text
持久化
生成 TraceID
上报 HTTP
修改路由
执行重试
更改业务异常
```

---

# 8. 数据模型

## 8.1 GatewayRequestContext

```python
@dataclass
class GatewayRequestContext:
    gateway_name: str
    gateway_version: Optional[str]
    request_id: Optional[str]
    protocol: str
    route: str
    requested_model: Optional[str]
    user_id: Optional[str]
    session_id: Optional[str]
    message_id: Optional[str]
    app_name: Optional[str]
    business_scenario: Optional[str]
```

## 8.2 RouteDecision

```python
@dataclass
class RouteDecision:
    provider: Optional[str]
    channel_id: Optional[str]
    channel_type: Optional[str]
    requested_model: Optional[str]
    resolved_model: Optional[str]
    route_reason: Optional[str]
    policy_name: Optional[str]
    fallback_from_channel_id: Optional[str]
```

## 8.3 AttemptContext

```python
@dataclass
class AttemptContext:
    attempt_index: int
    provider: Optional[str]
    channel_id: Optional[str]
    channel_type: Optional[str]
    resolved_model: Optional[str]
    upstream_base_url_hash: Optional[str]
    timeout_ms: Optional[int]
```

## 8.4 NormalizedUsage

```python
@dataclass
class NormalizedUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    usage_source: Optional[str] = None
```

## 8.5 NormalizedCost

```python
@dataclass
class NormalizedCost:
    input_cost: Optional[float] = None
    output_cost: Optional[float] = None
    total_cost: Optional[float] = None
    currency: str = "USD"
    cost_source: Optional[str] = None
```

---

# 9. GATEWAY Span 属性

## 9.1 通用字段

```text
gateway.name
gateway.version
gateway.request_id
gateway.protocol
gateway.route
gateway.trace_origin
gateway.upstream_trace_present
gateway.span_role
```

## 9.2 Router Span 字段

```text
gateway.span_role = router

gateway.requested_model
gateway.resolved_model
gateway.provider
gateway.channel_id
gateway.channel_type
gateway.route_reason
gateway.policy_name
gateway.retry_count
gateway.fallback_count
gateway.attempt_count
gateway.cache_status
gateway.queue_duration_ms
gateway.auth_duration_ms
gateway.route_duration_ms
gateway.total_duration_ms
gateway.ttft_ms
gateway.final_http_status_code
gateway.final_error_type
gateway.final_error_category
```

## 9.3 Attempt Span 字段

```text
gateway.span_role = provider_attempt

gateway.attempt_index
gateway.provider
gateway.channel_id
gateway.channel_type
gateway.resolved_model
gateway.upstream_request_id
gateway.upstream_http_status_code
gateway.upstream_duration_ms
gateway.upstream_connect_duration_ms
gateway.upstream_ttft_ms
gateway.timeout_ms
gateway.retryable
gateway.error_type
gateway.error_category
gateway.error_message
gateway.finish_reason
```

## 9.4 Usage 字段

```text
usage.input_tokens
usage.output_tokens
usage.total_tokens
usage.cached_input_tokens
usage.reasoning_tokens
usage.cache_creation_tokens
usage.cache_read_tokens
usage.source
```

## 9.5 Cost 字段

```text
cost.input
cost.output
cost.total
cost.currency
cost.source
```

---

# 10. Gateway Event 规范

统一事件名：

```text
gateway.auth.started
gateway.auth.completed
gateway.auth.failed

gateway.route.started
gateway.route.selected
gateway.route.failed
gateway.model.remapped

gateway.cache.hit
gateway.cache.miss
gateway.cache.bypass

gateway.rate_limit.checked
gateway.rate_limit.rejected
gateway.queue.entered
gateway.queue.exited

gateway.attempt.started
gateway.attempt.failed
gateway.attempt.completed

gateway.retry.scheduled
gateway.fallback.selected

gateway.stream.started
gateway.stream.first_token
gateway.stream.completed
gateway.stream.cancelled

gateway.response.completed
gateway.response.failed
```

Event 属性最多使用：

```text
reason
attempt_index
channel_id
provider
resolved_model
delay_ms
error_category
http_status_code
```

Event 默认不保存原始 Payload。

---

# 11. 错误分类

统一错误分类：

```text
authentication
authorization
rate_limit
quota
timeout
connect_error
dns_error
tls_error
provider_4xx
provider_5xx
invalid_request
invalid_response
stream_interrupted
client_cancelled
gateway_internal
unknown
```

错误字段：

```text
gateway.error_type
gateway.error_category
gateway.error_message
gateway.retryable
```

错误消息必须：

```text
safe string
长度限制
敏感信息脱敏
Fail-closed
```

不得记录：

```text
Authorization
API Key
Cookie
Provider Secret
完整 URL Query
完整响应体
完整堆栈
```

---

# 12. Usage 与 Cost 所有权

## 12.1 Attempt 级别

每个 Attempt 保存：

```text
本次请求消耗
本次请求费用
本次请求错误
本次请求延迟
```

失败 Attempt 如果 Provider 返回 Usage，也应记录。

## 12.2 Router 汇总

Router 保存：

```text
所有 Attempt Usage 汇总
所有 Attempt Cost 汇总
成功 Attempt
失败 Attempt 数量
最终渠道
```

## 12.3 LLM 汇总

SDK LLM 保存逻辑调用最终 Usage。

如果 Router 存在：

```text
LLM usage = Router 汇总 Usage
```

不得只保存最终成功 Attempt 的 Usage，否则重试产生的实际费用会丢失。

---

# 13. 路由、重试与降级

## 13.1 Route 选择

```python
router.route_selected(
    provider="openai",
    channel_id="channel-12",
    resolved_model="gpt-5.6",
    reason="weighted-random",
)
```

记录：

```text
gateway.route.selected
```

## 13.2 Retry

第一次失败：

```python
with router.attempt(...) as attempt:
    ...
```

失败后：

```python
router.retry_scheduled(
    attempt_index=1,
    delay_ms=200,
    reason="provider_5xx",
)
```

新建 Attempt 2。

## 13.3 Fallback

```python
router.fallback_selected(
    from_channel_id="channel-a",
    to_channel_id="channel-b",
    reason="timeout",
)
```

禁止仅增加：

```text
retry_count += 1
```

而不记录渠道切换。

---

# 14. Cache 与限流

## 14.1 Cache

支持：

```text
hit
miss
bypass
error
```

命中 Cache 时：

```text
Router 存在
不创建 Provider Attempt
gateway.cache_status = hit
gateway.attempt_count = 0
```

## 14.2 Rate Limit

拒绝请求：

```text
Router status = ERROR
gateway.error_category = rate_limit
gateway.attempt_count = 0
```

未发起上游请求时不得创建虚假 Attempt。

---

# 15. Streaming

## 15.1 TTFT

第一次有效内容到达时记录：

```text
gateway.stream.first_token
gateway.ttft_ms
gateway.upstream_ttft_ms
```

只记录一次。

## 15.2 完整消费

完整消费后：

```text
gateway.stream.completed
status = OK
```

## 15.3 客户端断开

客户端断开：

```text
gateway.stream.cancelled
gateway.error_category = client_cancelled
status != 普通 provider error
```

Attempt 和 Router 均必须结束。

## 15.4 资源清理

任何路径都必须清理：

```text
ContextVar
Span Registry
Streaming Wrapper Reference
Attempt Registry
Router Registry
Background Task
HTTP Session Handle
```

---

# 16. Privacy

默认禁止记录：

```text
Authorization Header
API Key
Cookie
Set-Cookie
原始渠道密钥
完整 upstream URL
完整 Prompt
完整 Response
Tool Input
Tool Output
用户上传文件
```

允许记录：

```text
Provider 名称
Channel ID 哈希
Model 名称
HTTP Status
Usage
Cost
Error Category
Request ID
```

Channel ID 推荐：

```text
内部原始 ID → HMAC / Hash
```

不得直接保存 Secret Name 或 Token。

---

# 17. Sampling

## 17.1 上游采样继承

存在合法 `traceparent`：

```text
trace_flags=01 → 采样
trace_flags=00 → 不上报
```

不得在网关重新随机采样覆盖上游决定。

## 17.2 无上游 Trace

无 SDK Trace 时：

```text
Gateway 根据本地 sample_rate 创建 Root Router
```

## 17.3 Sampled=0

即使不采样：

```text
业务必须正常
仍可传播 traceparent
不得执行大 Payload 序列化
不得生成 Reporter Record
```

---

# 18. Fail-open

任何观测异常不得改变网关业务行为。

覆盖：

```text
Router Span 创建失败
Attempt Span 创建失败
Event 添加失败
Usage 解析失败
Cost 计算失败
Span End 失败
Reporter 失败
Context Reset 失败
Streaming Finalization 失败
```

语义：

```text
业务成功 + Telemetry 失败
→ 返回业务成功

业务异常 + Telemetry 失败
→ 保留原业务异常
```

---

# 19. One-API Adapter

## 19.1 接入原则

```text
不修改 One-API 核心语义
不把 llm-observability 作为 One-API 强依赖
通过 Hook / Middleware / 胶水层接入
```

## 19.2 映射关系

```text
One-API Request Token
→ Association user/session/app

One-API Channel
→ gateway.channel_id / channel_type / provider

Model Mapping
→ gateway.requested_model / resolved_model

Relay Mode
→ gateway.protocol

Retry
→ Attempt Span + retry event

Fallback
→ fallback event + 新 Attempt

Quota
→ Usage / Cost

Upstream Response
→ Attempt result
```

## 19.3 适配边界

One-API Adapter 不允许：

```text
改变 Channel 选择
修改 Retry 次数
修改超时
修改 Quota
捕获并吞掉业务异常
```

---

# 20. UI 需求

## 20.1 Trace Detail

Router 节点显示：

```text
Gateway 名称
请求模型
最终模型
最终 Provider
最终 Channel
重试次数
降级次数
总耗时
TTFT
总 Token
总 Cost
```

## 20.2 Attempt Timeline

```text
Attempt 1
channel-a
500
420 ms
provider_5xx

Attempt 2
channel-a
timeout
5000 ms

Attempt 3
channel-b
200
830 ms
```

## 20.3 路由决策

显示：

```text
requested_model → resolved_model
route reason
fallback path
cache status
rate-limit result
```

## 20.4 Cost Breakdown

```text
Attempt 1：$0.003
Attempt 2：$0.000
Attempt 3：$0.007
Total：$0.010
```

---

# 21. Core 与存储需求

现有 Span 表继续使用。

新增或保证索引字段：

```text
span_kind
trace_id
parent_span_id
attributes.gateway.span_role
attributes.gateway.provider
attributes.gateway.channel_id
attributes.gateway.requested_model
attributes.gateway.resolved_model
attributes.gateway.error_category
```

如果 JSON 查询性能不足，可增加物化列：

```text
gateway_span_role
gateway_provider
gateway_channel_id
gateway_requested_model
gateway_resolved_model
gateway_error_category
```

Phase 3 首版可先使用 JSON Attribute，性能验证后再物化。

---

# 22. 指标需求

按 Provider：

```text
gateway_requests_total
gateway_attempts_total
gateway_errors_total
gateway_retries_total
gateway_fallbacks_total
gateway_cache_hits_total
```

延迟：

```text
gateway_total_duration_ms
gateway_upstream_duration_ms
gateway_ttft_ms
gateway_queue_duration_ms
```

Usage：

```text
gateway_input_tokens
gateway_output_tokens
gateway_total_tokens
```

Cost：

```text
gateway_total_cost
gateway_wasted_retry_cost
```

维度：

```text
provider
channel_id
requested_model
resolved_model
error_category
status
```

禁止使用高基数字段：

```text
request_id
user_id
session_id
trace_id
message_id
```

作为 Metrics Label。

---

# 23. 真实 E2E 验收

## Scenario A：一次成功

```text
AGENT
└── LLM
    └── Router
        └── Attempt 1 success
```

断言：

```text
1 AGENT
1 LLM
1 Router GATEWAY
1 Attempt GATEWAY
全部 TraceID 相同
Router.parent = LLM
Attempt.parent = Router
```

## Scenario B：Retry

```text
Attempt 1：500
Attempt 2：200
```

断言：

```text
2 Attempt
Attempt 1 ERROR
Attempt 2 OK
Router.retry_count = 1
Router status = OK
```

## Scenario C：Fallback

```text
channel-a timeout
→ channel-b success
```

断言：

```text
fallback event == 1
from_channel_id != to_channel_id
Router.final_channel = channel-b
```

## Scenario D：Cache Hit

```text
Router
无 Attempt
```

断言：

```text
gateway.cache_status = hit
gateway.attempt_count = 0
```

## Scenario E：Rate Limit

断言：

```text
Router ERROR
error_category = rate_limit
无 Attempt
```

## Scenario F：Streaming Success

断言：

```text
first_token event == 1
TTFT > 0
Router 和 Attempt Duration 覆盖完整消费
```

## Scenario G：Streaming Cancel

断言：

```text
client_cancelled
Router/Attempt 均结束
Context/Registry 无残留
```

## Scenario H：无 SDK Trace

断言：

```text
Router 为 Root
Attempt.parent = Router
无伪造 LLM
```

## Scenario I：Sampling=0

断言：

```text
无 Reporter Record
业务结果正常
上游采样决定未被覆盖
```

## Scenario J：隐私

断言以下内容不存在于所有 Span、Event、日志：

```text
API Key
Authorization
Cookie
完整 Prompt
完整 Response
Provider Secret
```

---

# 24. 单元测试

建议新增：

```text
tests/gateway_observability/
├── test_router_span.py
├── test_attempt_span.py
├── test_retry.py
├── test_fallback.py
├── test_streaming.py
├── test_usage.py
├── test_cost.py
├── test_privacy.py
├── test_sampling.py
├── test_fail_open.py
├── test_registry_cleanup.py
└── test_oneapi_adapter.py
```

必须覆盖故障注入：

```text
span.start 失败
span.end 失败
set_attribute 失败
add_event 失败
usage parser 失败
cost calculator 失败
reporter 失败
context reset 失败
stream close 失败
```

---

# 25. CI

新增 Job：

```text
gateway-runtime-tests
oneapi-adapter-tests
gateway-streaming-tests
gateway-real-e2e
phase2-regression
```

真实 E2E：

```text
仅受信任分支
Secret 缺失必须失败
Fork PR 禁止执行 Secret Job
日志必须脱敏
```

---

# 26. 推荐实施顺序

```text
Step 0  Gateway Contract Compatibility Spike
Step 1  Gateway Request Context / Data Model
Step 2  Router GATEWAY Runtime
Step 3  Attempt GATEWAY Runtime
Step 4  Retry / Fallback Event
Step 5  Streaming Lifecycle
Step 6  Usage Normalizer
Step 7  Cost Calculator
Step 8  Privacy / Sampling / Fail-open
Step 9  Generic GatewayAdapter
Step 10 One-API Adapter
Step 11 Core / UI 展示
Step 12 Real E2E
Step 13 Phase 2.1～2.5 Regression
```

---

# 27. OpenSpec Tasks

建议任务拆分：

```text
1. Gateway Contract
  1.1 Router / Attempt 层级
  1.2 Span 属性规范
  1.3 Event 规范
  1.4 Usage / Cost 所有权
  1.5 Streaming 生命周期

2. Gateway Runtime
  2.1 Context
  2.2 RouterSpan
  2.3 AttemptSpan
  2.4 Registry
  2.5 Fail-open
  2.6 Sampling

3. Retry / Fallback
  3.1 Retry
  3.2 Fallback
  3.3 Error Classification

4. Usage / Cost
  4.1 OpenAI Usage
  4.2 Anthropic Usage
  4.3 OpenAI-compatible Usage
  4.4 Cost Aggregation

5. One-API Adapter
  5.1 Request Mapping
  5.2 Channel Mapping
  5.3 Retry Mapping
  5.4 Streaming Mapping
  5.5 Usage Mapping

6. Core / UI
  6.1 Router Detail
  6.2 Attempt Timeline
  6.3 Cost Breakdown
  6.4 Gateway Filters

7. E2E
  7.1 Success
  7.2 Retry
  7.3 Fallback
  7.4 Streaming
  7.5 No SDK
  7.6 Privacy
```

---

# 28. Definition of Done

> 本节为 **Phase 3.0 — Gateway Contract & Runtime** 的 DoD。One-API 真实生命周期
> 接入属 Phase 3.1，Gateway 专用 UI/Metrics 属 Phase 3.2（见 §2.1），不在本节
> 验收范围内。

只有全部满足以下条件，Phase 3.0 才能冻结：

```text
1. Router GATEWAY 与 Attempt GATEWAY 语义冻结
2. SDK LLM → Router → Attempt 父子链正确
3. 无 SDK 请求可创建独立 Gateway Trace
4. 每次真实 Provider 请求对应唯一 Attempt
5. Retry 不覆盖旧 Attempt
6. Fallback 有明确 from/to 与原因
7. Streaming Span 覆盖完整消费周期
8. Client Cancel 不造成 Context/Registry 泄漏
9. Usage 在 Attempt、Router、LLM 三层所有权一致
10. Cost 包含失败重试产生的真实费用
11. One-API 通过 Adapter 字段映射接入（真实生命周期接入属 Phase 3.1）
12. LiteLLM Adapter 接口可扩展
13. 所有隐私字段默认不进入 Telemetry
14. 所有 Telemetry 故障完整 Fail-open
15. Sampling 正确继承
16. Streaming Terminal 为原子状态机（first terminal claim wins）
17. Hedged/Parallel 最终状态由显式 Winner 决定；无 Winner 有确定性 fail-safe
18. Terminal Event 互斥（attempt/response/stream 组内至多一个）
19. Streaming Duration 字段语义统一（完整上游流生命周期）
20. Success/Retry/Fallback/Streaming/No-SDK Real E2E 全部通过
21. Phase 2.1～2.5 Regression 全部通过
22. GitHub CI 全部成功
23. 无 skipped 的 Phase 3 必需验收测试
```

> 第 16 节（原 Core/UI 展示 Router 与 Attempt Timeline）移入 Phase 3.2，不再
> 作为 Phase 3.0 DoD。

完成后标记：

```text
Phase 3.0 — Gateway Contract & Runtime
✅ COMPLETE
✅ FROZEN

Phase 3.1 — One-API Production Integration
❌ PENDING

Phase 3.2 — Gateway UI & Metrics
❌ PENDING
```

只有 Phase 3.1、3.2 也完成时，方可标记：

```text
Phase 3 — Gateway Native Observability
✅ COMPLETE
✅ FROZEN
```

---

# 29. 本阶段禁止事项

```text
新增 Router/Provider SpanKind
修改 One-API 路由行为
吞掉网关业务异常
将 Retry 合并到一个 Attempt
将最终成功 Attempt Usage 当作全部 Usage
在流响应对象返回时提前结束 Span
保存 Authorization / API Key / Cookie
默认保存完整 Prompt / Response
以 TraceID/UserID 作为 Metrics Label
把 One-API 类型写死到 Gateway Runtime
未完成 Real E2E 就标记 COMPLETE
```

---

# 30. 第一轮交付物

第一轮只交付：

```text
1. Gateway Contract 文档
2. Router / Attempt Runtime 骨架
3. Generic GatewayAdapter 接口
4. Success + Retry Mock E2E
5. Privacy / Sampling / Fail-open 单测
```

暂不交付：

```text
One-API 完整 Adapter
UI
Cost 定价表
Anthropic Messages
OpenAI Responses
```

第一轮完成后再进入：

```text
Phase 3.1 — One-API Adapter Implementation
```
