# LLM Observability MVP 修复需求与问题清单

> 适用仓库：`xjy486/llm-observability`  
> 文档目的：作为下一轮修复与重构的直接输入，交由开发 Agent 按优先级逐项处理。  
> 当前结论：现有实现总体架构方向正确，不建议推倒重来；应先修复底层语义与工程问题，再继续开发 SDK、Agent Trace、LiteLLM Native OTel 等后续能力。

---

## 1. 总体目标

本轮工作的目标不是继续扩展功能，而是把当前 MVP 从“可演示”修正为“语义正确、可继续演进”的工程底座。

修复完成后，应满足以下核心原则：

1. `docker compose up` 可以直接启动完整链路。
2. Trace Context 可以从上游完整传播到 Proxy，再继续传播到 Gateway。
3. Streaming 与非 Streaming 的延迟指标语义准确。
4. Streaming 响应能够聚合为可查看的最终 Assistant 输出，而不是只保存 chunk 数。
5. Dashboard 区分 Trace 指标、LLM 调用指标与 Span 指标，不能把 Span 数直接当请求数。
6. Trace 查询筛选不能破坏完整 Trace 聚合。
7. 当前自定义 `/api/v1/ingest` 可以保留作为 MVP 内部接口，但架构必须为真正的 OTLP 接入留出明确边界。
8. One-API 仍只是第一个 Gateway，任何核心数据模型和 UI 不应绑定 One-API。

---

# 2. P0：必须优先修复

## P0-01 Docker Compose 环境变量与端口配置不一致

### 问题

当前 `docker-compose.yml` 使用：

```yaml
LISTEN_HOST=0.0.0.0
LISTEN_PORT=8082
OBSERVABILITY_URL=http://core:8001
```

但 `ProxyConfig.from_env()` 实际读取：

```text
PROXY_HOST
PROXY_PORT
OBSERVABILITY_ENDPOINT
```

导致：

- Proxy 可能仍监听默认 `8080`；
- Docker 映射却是 `8082:8082`；
- `OBSERVABILITY_URL` 不生效；
- Proxy 在容器内可能回退到 `http://localhost:8001`，错误地访问自身而不是 Core。

### 需求

统一配置字段，建议最终只保留：

```yaml
PROXY_HOST=0.0.0.0
PROXY_PORT=8082
UPSTREAM_URL=http://host.docker.internal:3000
OBSERVABILITY_ENDPOINT=http://core:8001
PAYLOAD_STRATEGY=masked
```

同时确保：

- Proxy Dockerfile `EXPOSE` 与实际监听端口一致；
- README 示例与代码读取字段一致；
- 本地运行和 Docker Compose 使用同一套配置名称。

### 验收标准

执行：

```bash
docker compose up -d
```

后：

1. `curl http://localhost:8082/health` 返回健康状态；
2. Proxy 能访问 Core；
3. Client 通过 `http://localhost:8082/v1/chat/completions` 能正常转发到 One-API；
4. Trace 能在 Core 中成功入库并从 UI 查询到。

---

## P0-02 W3C Trace Context 传播断链

### 问题

当前 Proxy 能正确解析上游 `traceparent`：

```text
上游 trace_id
上游 span_id
↓
Proxy 创建新 span_id
```

但转发请求给 Gateway 时，将原始 `traceparent` 删除，却没有注入新的：

```text
traceparent = 00-{trace_id}-{proxy_span_id}-{flags}
```

结果：

```text
Application Trace
    ↓
Proxy 能看到同一 Trace
    ↓
One-API / LiteLLM 收不到 Trace Context
```

未来无法实现：

```text
AGENT
└── LLM
    └── Proxy
        └── Gateway
            ├── Routing
            ├── Retry
            └── Provider
```

同一个 Trace 的端到端串联。

### 需求

Proxy 在解析或创建 `TraceContext` 后，必须把新的 `traceparent` 注入下游请求：

```python
forward_headers["traceparent"] = trace_ctx.to_traceparent()
```

语义要求：

- 有上游 Trace：继承原 `trace_id`，当前 Proxy Span 成为上游 Span 的 child；
- 无上游 Trace：Proxy 创建新的 root Trace；
- Gateway 收到的 `parent-id` 必须是 Proxy 当前 Span ID；
- 不直接把旧的上游 `traceparent` 原样转发。

### 验收标准

#### Case A：无上游 Trace

```text
Client
→ Proxy 自动创建 Trace A / Span P
→ Gateway 收到 traceparent(trace=A, parent=P)
```

#### Case B：有上游 Trace

```text
Agent Span A1
→ Proxy Span P1
→ Gateway Span G1
```

三者：

```text
trace_id 相同
A1 -> P1 -> G1 父子关系正确
```

增加自动化测试验证 Header。

---

## P0-03 Streaming 响应缺少最终 Assistant 输出聚合

### 问题

当前 Streaming 模式会收集 SSE chunk，但最终 Payload 只保存类似：

```json
{
  "stream_chunks_summary": {
    "chunk_count": 37
  },
  "usage": {},
  "model": "xxx"
}
```

即使 `PAYLOAD_STRATEGY=full`，也不能像 AgentLens 一样查看完整最终输出。

### 需求

新增 Streaming Response Accumulator。

不要长期保存所有原始 SSE chunk，而是增量聚合为标准化响应：

```json
{
  "model": "...",
  "assistant": {
    "role": "assistant",
    "content": "...完整文本...",
    "tool_calls": []
  },
  "finish_reason": "...",
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

需要支持至少：

- `delta.content`
- `delta.reasoning_content`（若存在）
- `delta.tool_calls`
- `finish_reason`
- `usage`
- `model`

### 约束

- `metadata_only`：不保存内容，只保留结构信息；
- `masked`：聚合后做脱敏；
- `full`：保存聚合后的完整响应；
- `off`：完全不保存 Payload；
- 不应为了观测阻塞客户端 SSE 转发。

### 验收标准

Streaming 请求结束后，在 Trace Detail 中可以查看：

```text
Input:
system
user
tools

Output:
assistant 完整文本
tool_calls（若存在）
usage
finish_reason
```

而不是只能看到 `chunk_count`。

---

## P0-04 TTFT / TTFC 指标语义修正

### 问题 A：非流式请求把总耗时当 TTFT

当前类似：

```python
ttft_ms = elapsed_ms
```

这是错误语义。

非流式响应无法知道模型第一个 Token 的真实生成时间。

### 需求 A

非流式：

```text
ttft_ms = null
```

只能记录：

```text
total_latency_ms
```

### 问题 B：Streaming 当前测到的是第一个可解析 SSE chunk

第一个 chunk 可能只有：

```json
{
  "delta": {
    "role": "assistant"
  }
}
```

并没有实际 Token 或文本。

### 需求 B

必须明确产品指标。

推荐同时定义：

#### TTFC

```text
Time To First Chunk
```

记录第一个有效 SSE data chunk。

#### TTFT

```text
Time To First Token
```

记录首次出现以下任一有效内容：

- 非空 `delta.content`
- 非空 reasoning content
- 首个 tool call delta

数据模型建议同时支持：

```text
ttfc_ms
ttft_ms
```

MVP 若只保留一个，必须在代码、API、UI 和 README 中统一命名，不得把 TTFC 错称 TTFT。

### 验收标准

- 非流式请求：`ttft_ms = null`
- Streaming 第一个 role-only chunk 不触发 TTFT
- 第一个实际内容 chunk 触发 TTFT
- Dashboard 不把非流式总延迟混入 TTFT 统计

---

## P0-05 Dashboard 指标必须区分 Trace / LLM Call / Span

### 问题

当前指标直接：

```sql
COUNT(*) FROM spans
```

并把结果展示成：

```text
Total Requests
```

当前 Proxy 场景：

```text
1 Request = 1 Trace = 1 Span
```

暂时看不出问题。

未来 Agent Trace：

```text
1 Trace
├── 1 AGENT Span
├── 4 LLM Span
└── 3 TOOL Span
```

会被错误统计成：

```text
Total Requests = 8
```

### 需求

明确至少三种统计语义。

#### Trace Metrics

```text
trace_count
trace_error_count
trace_error_rate
trace_p50_latency
trace_p95_latency
trace_p99_latency
```

#### LLM Metrics

```text
llm_call_count
llm_error_count
llm_error_rate
llm_p50_latency
llm_p95_latency
llm_p99_latency
avg_ttft
p50_ttft
p95_ttft
input_tokens
output_tokens
total_tokens
```

#### Span Metrics

只用于调试或内部系统，不作为“请求数”。

### UI 建议

Dashboard 至少展示：

```text
业务任务数 / Trace Count
LLM 调用次数
错误率
P50/P95/P99 LLM Latency
TTFT
Tokens
```

### 验收标准

构造：

```text
1 Trace
8 Spans
4 LLM Spans
```

Dashboard 应显示：

```text
Trace Count = 1
LLM Calls = 4
```

而不是 8。

---

## P0-06 Trace 筛选不能先过滤 Span 再聚合

### 问题

当前查询类似：

```sql
SELECT ...
FROM spans
WHERE model = ?
GROUP BY trace_id
```

例如完整 Trace：

```text
Trace ABC
├── AGENT
├── LLM gpt-5.6
├── TOOL
├── LLM gpt-5.6
├── TOOL
└── LLM claude
```

筛选：

```text
model = gpt-5.6
```

当前会先留下两个 LLM Span，再聚合。

于是返回的 Trace Summary 会错误变成：

```text
span_count = 2
duration = 只覆盖两个 LLM Span
token = 只统计筛选后的 Span
```

完整 Trace 被“切残”。

### 需求

筛选分两步。

#### Step 1：找匹配的 Trace ID

```sql
SELECT DISTINCT trace_id
FROM spans
WHERE model = ?
```

#### Step 2：重新读取完整 Trace

```sql
SELECT ...
FROM spans
WHERE trace_id IN (...)
GROUP BY trace_id
```

筛选条件用于决定“哪些 Trace 被命中”，不能改变 Trace 本身的聚合结果。

### 验收标准

同一个 6 Span Trace：

```text
按 model=gpt-5.6 筛选
```

返回后仍然：

```text
span_count = 6
duration = 完整 Trace duration
tokens = 完整 Trace 的全部 LLM Token 汇总
```

---

## P0-07 Pagination total 返回错误

### 问题

当前：

```python
total = len(results)
```

例如：

```text
数据库 300 条 Trace
limit = 50
```

第一页会返回：

```text
total = 50
```

导致前端误判已经是最后一页。

### 需求

增加独立 count 查询：

```sql
COUNT(DISTINCT trace_id)
```

返回：

```json
{
  "traces": ["...50 items..."],
  "total": 300,
  "limit": 50,
  "offset": 0
}
```

### 验收标准

存在 120 条 Trace 时：

```text
limit=50
```

分页应为：

```text
1-50 / 120
51-100 / 120
101-120 / 120
```

---

# 3. P1：P0 修完后立即处理

## P1-01 从“OpenTelemetry 风格 JSON”演进到真正 OTLP 边界

### 当前状态

当前 Proxy 自己构造：

```json
{
  "trace_id": "...",
  "span_id": "...",
  "attributes": {
    "gen_ai.request.model": "..."
  }
}
```

再调用：

```text
POST /api/v1/ingest
```

这属于：

```text
OpenTelemetry semantics
```

但不是：

```text
OTLP
```

### 风险

未来 LiteLLM、Application SDK、Gateway Native 可能原生输出 OTLP。

如果 Core 只支持自研 `/api/v1/ingest`：

```text
LiteLLM OTLP
→ 还要再写转换器
→ 公共协议继续耦合
```

### 需求

长期公共入口应收敛为：

```text
OTLP
```

推荐架构：

```text
Proxy ─────────────┐
Application SDK ───┼──► OTel Collector / OTLP Ingestion
LiteLLM Native ────┤
Gateway Native ────┘
                         ↓
                     Normalizer
                         ↓
                 Observability Core
```

### 兼容要求

当前：

```text
POST /api/v1/ingest
```

可以暂时保留作为：

```text
MVP internal ingestion protocol
```

但不得继续把它定义为最终公共标准。

### 本轮建议

至少完成：

1. 新增架构文档，明确 OTLP 是最终公共边界；
2. 把自定义 JSON → Canonical Span 的转换逻辑集中到独立 `normalizer`；
3. 禁止 UI/Core 逻辑依赖 Proxy 特有字段；
4. 为后续接入 OTLP Receiver 预留模块边界。

---

## P1-02 Payload 真正与 Span Metadata 解耦

### 当前状态

虽然有：

```text
payload_ref
```

字段，但 Payload 实际仍直接存入 `spans.payload TEXT`。

### 风险

真实 Agent Prompt 可能非常大：

```text
system
tools schema
多轮 messages
源代码
完整 response
```

单条可达到几十 KB / 几百 KB。

如果全部放 spans 表：

- Trace List 查询变慢；
- 数据库膨胀；
- Payload 权限难隔离；
- 保留周期难独立控制。

### 需求

MVP 可保留 SQLite，但结构需要演进为：

```text
spans
├── metadata
└── payload_ref

payloads
├── payload_id
├── trace_id
├── span_id
├── request_payload
├── response_payload
└── created_at
```

或者抽象 Payload Repository。

### 验收标准

- Trace List 不读取完整 Payload；
- Trace Detail 只有用户展开时才读取 Payload；
- Payload 可以配置独立 retention；
- `payload_ref` 真正打通 Ingest Model → Storage → Query → UI。

---

## P1-03 Payload Masking 增加按 Key 脱敏

### 问题

当前主要依靠字符串正则。

例如：

```json
{
  "access_token": "abc123"
}
```

递归到 value 后只剩：

```text
abc123
```

未必匹配 `token:` 正则，可能泄漏。

### 需求

实现：

```text
Key-based Masking
+
Regex-based Content Masking
```

建议敏感 Key：

```text
authorization
password
passwd
token
access_token
refresh_token
api_key
apikey
secret
client_secret
cookie
set-cookie
```

### 规则

若 key 命中：

```json
{
  "access_token": "***MASKED***"
}
```

否则再对 string value 做正则扫描。

### 验收标准

新增单元测试覆盖：

```text
access_token
password
Authorization
nested token
list 中嵌套 secret
OpenAI sk-...
Bearer ...
```

---

## P1-04 Gateway 名称不能硬编码 One-API

### 问题

当前：

```text
llm.gateway.name = one-api-proxy
```

### 需求

配置化：

```yaml
GATEWAY_TYPE=one-api
GATEWAY_NAME=company-llm-gateway
```

语义建议：

```text
llm.gateway.type = one-api
llm.gateway.name = company-llm-gateway
```

换 LiteLLM：

```text
llm.gateway.type = litellm
```

Core 和 UI 不改。

---

## P1-05 Streaming 内存占用优化

### 问题

当前可能同时保留：

```text
stream_chunks[]
total_output bytes
```

长请求时内存随完整响应线性增长。

其中 `total_output` 当前并无实际用途。

### 需求

改为 Streaming Incremental Accumulator：

```text
model
content_buffer（仅需要保存 Payload 时）
reasoning_buffer
tool_calls accumulator
usage
finish_reason
chunk_count
```

### 规则

当：

```text
PAYLOAD_STRATEGY=off
```

不要保存完整 content buffer。

当：

```text
sample_rate 未命中
```

也不应无条件缓存所有原始 chunk。

---

# 4. P1：安全与工程化

## P1-06 Core Ingestion 与 Query 增加最小认证

### 当前风险

Core 暴露：

```text
POST /api/v1/ingest
GET /api/v1/traces
GET /api/v1/traces/{id}
```

同时 Payload 中可能包含：

```text
内部代码
用户数据
Prompt
Response
Tool 参数
```

### 需求

至少实现：

#### Ingestion Key

```http
Authorization: Bearer <OBSERVABILITY_API_KEY>
```

或：

```http
X-Observability-Api-Key
```

#### Query Auth

MVP 可先使用单一管理 Token。

后续再扩展：

```text
RBAC
Payload Sensitive Permission
Tenant
```

### 同时调整

生产模式不要默认：

```python
allow_origins=["*"]
```

需要配置化 CORS。

---

## P1-07 Reporter 增加重试退避与失败策略

### 当前问题

失败后立即 requeue，固定周期继续 flush。

Observability Core 长时间故障时可能持续打日志和反复请求。

### 需求

增加：

```text
exponential backoff
max retry / age
dropped count
queue saturation metrics
```

同时保持原则：

```text
Telemetry 永不阻塞主 LLM 调用
```

---

# 5. P2：完成底座修复后再开发

## P2-01 Application SDK

目标接入：

```python
from observe import Observe

Observe.init(
    app_name="agent_server",
    api_endpoint="...",
    api_key="..."
)
```

支持：

```text
OpenAI
AzureOpenAI
LangChain
Agent
Tool
```

产生：

```text
Trace
└── AGENT
    ├── LLM
    ├── TOOL
    ├── LLM
    └── TOOL
```

前提：

- Trace Context 传播已修复；
- Trace/Span Metrics 已区分；
- OTLP 边界已明确。

---

## P2-02 Gateway Native Instrumentation

One-API / LiteLLM 内部可选采集：

```text
Gateway
├── Auth
├── Routing
├── Channel Selection
├── Provider Request
├── Retry
└── Fallback
```

原则：

```text
不接 Native Instrumentation 也能用基础 Observability；
接入后只是看得更深。
```

---

# 6. 数据语义必须冻结

## 6.1 Trace 创建规则

### 有上游 Trace Context

```text
继承 TraceID
创建 Child Span
```

### 无上游 Trace Context

```text
创建新 Trace
当前 LLM Request 成为 Root/首个 Span
```

### 禁止

不得使用以下字段推断多个请求属于同一 Trace：

```text
SessionID
UserID
时间邻近性
相同 Model
相同 IP
```

---

## 6.2 Session 与 Trace

正确关系：

```text
User
└── Session
    ├── Trace A
    │   ├── Span
    │   └── Span
    ├── Trace B
    └── Trace C
```

不是：

```text
一个 Session = 一个 Trace
```

---

## 6.3 指标命名

必须严格区分：

```text
Trace Count
LLM Call Count
Span Count
```

不得统一叫：

```text
Request Count
```

除非明确其含义。

---

## 6.4 Latency

建议统一：

```text
trace_duration_ms
span_duration_ms
llm_latency_ms
ttfc_ms
ttft_ms
```

不要用同一个 `duration_ms` 在 UI 中表达不同业务含义而不说明。

---

# 7. 自动化测试要求

## 7.1 Trace Context

- 无 traceparent → 新建 Trace
- 有合法 traceparent → 继承 TraceID
- 非法 traceparent → 新建 Trace
- Proxy 转发时生成正确的新 traceparent
- parent-child 关系正确

## 7.2 Streaming

- 普通 content streaming
- role-only 首 chunk
- tool_call streaming
- usage 在最后 chunk
- 无 usage
- `[DONE]`
- 中途断流
- 客户端断开
- Payload off / metadata_only / masked / full

## 7.3 Metrics

构造：

```text
2 Traces
Trace A: 1 AGENT + 2 LLM + 1 TOOL
Trace B: 1 LLM
```

断言：

```text
trace_count = 2
llm_call_count = 3
span_count = 5
```

## 7.4 Trace Filtering

构造：

```text
Trace A:
AGENT
LLM gpt-5
TOOL
LLM claude
```

筛选：

```text
model=gpt-5
```

断言：

```text
Trace A 被返回
span_count 仍为 4
duration 仍为完整 Trace duration
```

## 7.5 Pagination

至少 120 条 Trace。

验证：

```text
limit 50
offset 0/50/100
total 始终为 120
```

## 7.6 Docker E2E

CI 或手动脚本至少验证：

```text
docker compose up
↓
Proxy health
↓
Core health
↓
Mock Gateway / One-API request
↓
Trace ingest
↓
Trace query
↓
Metrics query
```

---

# 8. 建议实施顺序

## Phase 1：先修正确性

1. P0-01 Docker 配置统一
2. P0-02 Trace Context 下游传播
3. P0-04 TTFT / TTFC 语义
4. P0-03 Streaming Response 聚合
5. P0-07 Pagination total

## Phase 2：修数据语义

6. P0-05 Trace / LLM / Span Metrics 分离
7. P0-06 Trace Filter 两阶段查询

## Phase 3：修长期架构

8. P1-01 OTLP / Normalizer 边界
9. P1-02 Payload 分离
10. P1-03 Masking
11. P1-04 Gateway 配置化
12. P1-05 Streaming 内存优化

## Phase 4：安全与稳定性

13. P1-06 Auth / CORS
14. P1-07 Reporter backoff

## Phase 5：再扩功能

15. Application SDK
16. Gateway Native Instrumentation

---

# 9. 本轮明确禁止事项

在上述 P0/P1 底座问题完成前，不要优先开发：

```text
AI 自动诊断
Prompt Evaluation
幻觉检测
告警中心
成本优化
多租户复杂 RBAC
大量新 Dashboard
更多模型 Provider 特判
```

也不要：

1. 把 Observability 逻辑重新塞入 One-API Core；
2. 为 One-API 单独设计新的核心 Trace 数据模型；
3. 使用 SessionID 自动合并 Trace；
4. 将自定义 `/api/v1/ingest` 永久定义为公共协议；
5. 为了“看起来像 AgentLens”先堆 UI，而不修 Trace 语义。

---

# 10. Definition of Done

本轮修复完成后，应达到：

```text
Client / Agent
      │
      │ traceparent optional
      ▼
Telemetry Proxy
      │
      ├── 正确创建/继承 Trace
      ├── 正确测量 Latency / TTFT
      ├── 正确聚合 Streaming
      ├── Payload 可控采集/脱敏
      └── 异步上报
      │
      ▼
One-API / LiteLLM
      │
      └── 收到正确的下游 traceparent
```

Observability Core：

```text
Trace Count 正确
LLM Call Count 正确
Span Count 正确
Trace Filtering 不切残
Pagination total 正确
Payload 可独立演进
```

并且：

```text
One-API → LiteLLM
```

时不需要重写：

```text
Trace Schema
Query API
Storage Core
Web UI
```

---

# 11. 最终验收场景

## 场景 A：无上游 Trace

```text
普通 OpenAI-compatible Client
→ Telemetry Proxy
→ One-API
→ LLM
```

结果：

```text
1 次 LLM Request
=
1 个自动创建的 Trace
=
1 个 LLM Span
```

可查看：

```text
Input
Output
Model
Latency
TTFT（Streaming）
Tokens
Error
```

## 场景 B：有上游业务 Trace

模拟：

```text
Agent Trace ABC
└── Agent Span A
    ↓ traceparent
Telemetry Proxy
    └── LLM Span L
        ↓ 新 traceparent
Gateway
```

最终至少保证：

```text
TraceID = ABC
Agent Span A
└── LLM Span L
```

未来增加 Native Instrumentation 后可继续扩展：

```text
Agent Span
└── LLM Span
    └── Gateway Span
        ├── Routing
        ├── Provider #1 ERROR
        └── Provider #2 OK
```

该模型必须无需修改 Observability Core 的基本 Trace/Span 语义即可成立。
