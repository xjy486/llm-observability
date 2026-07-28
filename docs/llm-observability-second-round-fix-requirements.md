# LLM Observability 第二轮修复需求文档

> 适用仓库：`xjy486/llm-observability`  
> 基线：最近两次提交 `221be4c`、`372e19c` 之后的 `main`  
> 目的：在上一轮 P0/P1 修复基础上，完成 MVP Foundation 的最后一轮正确性收口。  
> 原则：**本轮不要继续开发 Application SDK、Agent Trace、Gateway Native Instrumentation；先把现有基础能力修到语义一致、前后端契约一致、可升级、可验证。**

---

# 1. 本轮目标

当前仓库已经完成了大量有效修复，包括：

- Docker Compose 环境变量与端口统一；
- W3C `traceparent` 下游重新注入；
- StreamingAccumulator；
- Gateway Name 配置化；
- Key-based Masking；
- Trace / LLM / Span 指标后端分层；
- Trace 两阶段查询；
- Pagination total 基础修复。

但当前仍存在 4 个阻塞级问题和若干中等级问题：

1. **前后端 API Contract 已经不一致；**
2. **TTFT / TTFC 时间指标语义仍然错误；**
3. **Trace 筛选只修了一半，status / duration 仍不正确；**
4. **SQLite 缺少 schema migration，旧数据升级会失败；**
5. TimeSeries 指标口径仍未统一；
6. Pagination 当前实现对大规模 Trace 不可扩展；
7. `MASK_KEYS` 文档宣称可配置，但代码未真正读取环境变量；
8. Streaming 在不采集 Payload 时仍会缓存完整输出；
9. OTLP / Payload Store / Auth 等长期架构项仍未完成，但本轮只要求预留，不要求全部实现。

---

# 2. P0-NEW-01：修复前后端 API Contract 不一致

## 2.1 问题

Backend 已将 Dashboard Metrics 改为：

```json
{
  "trace_count": 0,
  "llm_call_count": 0,
  "span_count": 0,
  "error_count": 0,
  "error_rate": 0,
  "p50_latency_ms": 0,
  "p95_latency_ms": 0,
  "p99_latency_ms": 0,
  "avg_ttft_ms": null,
  "p50_ttft_ms": null,
  "p95_ttft_ms": null,
  "avg_ttfc_ms": null,
  "p50_ttfc_ms": null,
  "p95_ttfc_ms": null,
  "total_input_tokens": 0,
  "total_output_tokens": 0,
  "total_tokens": 0
}
```

但 Frontend 仍然使用旧接口：

```ts
interface MetricsSummary {
  total_requests: number
  ...
}
```

Dashboard 也仍然：

```tsx
metrics.total_requests
```

因此当前真实运行时存在：

```text
Backend 返回 trace_count
Frontend 读取 total_requests
→ undefined
```

TimeSeries 和 ModelInfo 也存在同类问题。

---

## 2.2 需求

### A. 同步 `MetricsSummary`

Frontend 改为：

```ts
export interface MetricsSummary {
  trace_count: number
  llm_call_count: number
  span_count: number

  error_count: number
  error_rate: number

  p50_latency_ms: number
  p95_latency_ms: number
  p99_latency_ms: number

  avg_ttft_ms: number | null
  p50_ttft_ms: number | null
  p95_ttft_ms: number | null

  total_input_tokens: number
  total_output_tokens: number
  total_tokens: number

  unique_models: number
  unique_users: number
  unique_sessions: number
}
```

### B. Dashboard 卡片调整

至少展示：

```text
Trace Count
LLM Calls
Error Rate
P50 Latency
P95 Latency
Avg TTFT
Total Tokens
```

`Span Count` 可放调试信息或次级区域，不要再叫 `Total Requests`。

### C. 同步 `TimeSeriesPoint`

Backend/Frontend 必须统一为：

```ts
interface TimeSeriesPoint {
  bucket: number

  trace_count: number
  llm_call_count: number
  span_count: number

  trace_error_count: number
  llm_error_count: number

  llm_avg_latency_ms: number | null
  tokens: number
  avg_ttft_ms: number | null
}
```

禁止继续使用含义模糊的：

```text
count
errors
avg_latency
```

### D. 同步 `ModelInfo`

建议：

```ts
interface ModelInfo {
  model: string
  trace_count: number
  llm_call_count: number
  span_count: number
  llm_errors: number
}
```

### E. 同步 `SpanRecord`

如果保留新增时间字段，Frontend 类型也必须同步。

---

## 2.3 验收标准

启动真实 Core + UI：

```text
Dashboard
```

必须能正确显示：

```text
Trace Count
LLM Calls
Error Rate
P50/P95
TTFT
Tokens
```

浏览器 Console 不应出现：

```text
undefined
NaN
API field mismatch
```

必须增加至少一组 Frontend/Backend Contract Test。

---

# 3. P0-NEW-02：重新冻结时间指标语义

这是本轮最重要的数据语义修复之一。

---

## 3.1 当前问题

当前代码把：

```text
ttft_ms
```

定义为：

```text
第一个可解析 SSE JSON chunk
```

但第一个 chunk 可能只有：

```json
{
  "delta": {
    "role": "assistant"
  }
}
```

没有真正 Token。

因此它实际上更接近：

```text
Time To First Chunk
```

而不是：

```text
Time To First Token
```

同时非流式请求仍然：

```text
ttft_ms = elapsed_ms
```

这也是错误的。

---

## 3.2 最终统一定义

本轮直接冻结以下字段。

### `duration_ms`

```text
请求开始
→
完整响应结束
```

适用于 Streaming 与 Non-streaming。

### `first_chunk_ms`

仅 Streaming：

```text
请求开始
→
收到第一个合法 SSE data chunk
```

注意：`role-only chunk` 也算 first chunk。

### `ttft_ms`

仅 Streaming：

```text
请求开始
→
首次出现实际可消费内容
```

触发条件为以下任意一个：

```text
delta.content 非空
delta.reasoning_content 非空
首个有效 tool_call delta
```

`role=assistant` 不触发 TTFT。

---

## 3.3 非流式请求规则

非流式：

```text
duration_ms = total latency
first_chunk_ms = null
ttft_ms = null
```

禁止：

```text
ttft_ms = duration_ms
```

---

## 3.4 删除或重命名当前 `ttfc_ms`

当前实现把 `TTFC` 解释成 `Time To Complete`，但 `duration_ms` 已经表达完整请求结束时间。

建议删除 `ttfc_ms`。

若确实需要“首 Chunk”指标，则改成明确字段：

```text
first_chunk_ms
```

不要继续使用模糊缩写 `TTFC`。

---

## 3.5 StreamingAccumulator 增加首内容检测

建议新增：

```python
def has_meaningful_output(chunk: dict) -> bool:
    ...
```

逻辑：

```text
content 非空
OR reasoning_content 非空
OR tool_calls 中出现有效 name/id/arguments
```

Handler：

```text
收到第一个合法 chunk
→ first_chunk_ms

收到第一个 meaningful output
→ ttft_ms
```

---

## 3.6 验收测试

### Case 1

```text
chunk 1: role=assistant
chunk 2: content=""
chunk 3: content="你"
```

结果：

```text
first_chunk_ms = chunk1 时间
ttft_ms = chunk3 时间
```

### Case 2：tool call

```text
chunk1 role
chunk2 tool_call function.name
```

结果：

```text
TTFT = chunk2
```

### Case 3：非流式

```text
ttft_ms = null
first_chunk_ms = null
duration_ms > 0
```

---

# 4. P0-NEW-03：Trace Filter 语义完整修复

当前“两阶段查询”方向是对的，但过滤语义仍然不完整。

---

## 4.1 问题一：`min_duration_ms / max_duration_ms` 参数被静默忽略

API 仍接受：

```text
min_duration_ms
max_duration_ms
```

但 Storage Phase 1 没有使用。

因此用户传参后没有效果。

---

## 4.2 问题二：Trace Status 不能按 Span Status 直接筛

当前：

```sql
SELECT DISTINCT trace_id
FROM spans
WHERE status = 'OK'
```

语义错误。

例如：

```text
Trace A
├── AGENT OK
├── LLM OK
├── TOOL ERROR
└── LLM OK
```

完整 Trace 状态：

```text
ERROR
```

但 `status=OK` 会因为 Trace 内存在 OK Span 而命中。

---

## 4.3 过滤条件分类

必须区分：

### Span-level EXISTS Filter

表示 Trace 中存在至少一个满足条件的 Span。

例如：

```text
model
span_kind
provider
```

实现示意：

```sql
EXISTS (
  SELECT 1
  FROM spans s2
  WHERE s2.trace_id = t.trace_id
    AND s2.model = ?
)
```

### Trace-level Aggregate Filter

必须在完整 Trace 聚合后判断：

```text
status
duration
trace start/end
```

例如：

```sql
GROUP BY trace_id
HAVING trace_duration >= ?
```

Trace Status：

```text
只要任意 Span ERROR
→ Trace ERROR
否则 OK
```

---

## 4.4 推荐实现

建议使用 CTE：

```sql
WITH trace_agg AS (
  SELECT
    trace_id,
    MIN(start_time) AS start_time,
    MAX(end_time) AS end_time,
    (MAX(end_time) - MIN(start_time)) * 1000 AS duration_ms,
    CASE
      WHEN SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) > 0
      THEN 'ERROR'
      ELSE 'OK'
    END AS trace_status
  FROM spans
  GROUP BY trace_id
),
candidates AS (
  SELECT ...
)
```

然后组合：

```text
Trace-level conditions
+
EXISTS span-level conditions
```

---

## 4.5 验收标准

### Status

构造：

```text
Trace A: OK/ERROR mixed
Trace B: all OK
```

查询：

```text
status=OK
```

只能返回 Trace B。

### Duration

```text
Trace A = 10s
Trace B = 3s
```

查询：

```text
min_duration_ms=5000
```

只能返回 Trace A。

### Model

Trace A：

```text
AGENT
LLM gpt-5
TOOL
LLM claude
```

查询：

```text
model=gpt-5
```

返回 Trace A，但：

```text
span_count
duration
tokens
```

必须仍基于完整 Trace。

---

# 5. P0-NEW-04：增加 SQLite Schema Migration

## 5.1 问题

新版本新增字段后只使用：

```sql
CREATE TABLE IF NOT EXISTS spans (...)
```

旧数据库不会自动新增列。

用户如果已有 Docker Volume：

```text
旧 spans 表
↓
升级代码
↓
新 Insert 包含新字段
```

可能报：

```text
no column named xxx
```

---

## 5.2 最低要求

初始化时读取：

```sql
PRAGMA table_info(spans)
```

检查字段。

例如：

```python
if "first_chunk_ms" not in columns:
    ALTER TABLE spans ADD COLUMN first_chunk_ms REAL
```

如决定删除 `ttfc_ms`，需兼容已有旧列，不要求立刻删除。

---

## 5.3 Schema Version

建议新增：

```text
schema_version
```

最简单可以是一张：

```sql
metadata(
  key TEXT PRIMARY KEY,
  value TEXT
)
```

记录：

```text
schema_version=2
```

---

## 5.4 Ingest 错误返回

当前不能出现：

```text
10 条都 insert 失败
HTTP 200
status=ok
inserted=0
```

建议：

### 全成功

```json
{
  "status": "ok",
  "inserted": 10,
  "failed": 0
}
```

### 部分失败

```json
{
  "status": "partial",
  "inserted": 7,
  "failed": 3
}
```

### 全失败

返回非 2xx。

至少不能伪装为成功。

---

## 5.5 验收标准

准备一个旧版 DB：

```text
没有新增列
已有 Trace 数据
```

升级新版本启动：

```text
旧数据仍可查询
新请求可正常写入
无手工删库要求
```

---

# 6. P1-NEW-01：TimeSeries 指标口径与 Summary 保持一致

## 当前问题

Summary Metrics 已经区分：

```text
Trace
LLM
Span
```

但 TimeSeries 仍然：

```sql
AVG(duration_ms)
SUM(error spans)
```

混合所有 Span。

## 需求

TimeSeries 改成明确字段：

```json
{
  "bucket": 0,
  "trace_count": 0,
  "trace_error_count": 0,
  "llm_call_count": 0,
  "llm_error_count": 0,
  "llm_avg_latency_ms": 0,
  "avg_ttft_ms": null,
  "span_count": 0,
  "tokens": 0
}
```

前端 Chart 与这些字段一一对应。

禁止继续使用：

```text
count
errors
avg_latency
```

这种没有层级语义的字段。

---

# 7. P1-NEW-02：Pagination 改成可扩展实现

## 当前问题

当前：

```text
SELECT DISTINCT trace_id
→ 全部加载进 Python
→ len()
→ 所有 trace_id 再塞回 IN (?, ?, ...)
```

小数据正确，大数据会有：

```text
内存压力
SQLite bind 参数限制
SQL 过长
```

## 需求

基于 CTE / 子查询完成：

```text
candidate traces
      │
      ├── COUNT(*) → total
      │
      └── ORDER/LIMIT/OFFSET
              ↓
        当前页 trace_ids
              ↓
        聚合完整 spans
```

不要把全部 TraceID 拉到 Python。

---

# 8. P1-NEW-03：`MASK_KEYS` 环境变量真正生效

## 问题

README 宣称支持：

```text
MASK_KEYS=api_key,secret,...
```

但 `ProxyConfig.from_env()` 没有读取。

## 需求

实现：

```python
custom_mask_keys = [
    x.strip()
    for x in os.getenv("MASK_KEYS", "").split(",")
    if x.strip()
]
```

并与默认值合并：

```text
default mask keys
+
custom env keys
```

不要用环境变量覆盖掉基础安全默认值。

---

# 9. P1-NEW-04：Streaming 在不采集 Payload 时不得缓存完整输出

## 当前问题

即使：

```text
PAYLOAD_STRATEGY=off
```

或 Sampling 未命中，Accumulator 仍然：

```text
content_parts.append(...)
reasoning_parts.append(...)
tool_call arguments += ...
```

长输出仍会占用完整内存。

## 需求

StreamingAccumulator 增加模式：

```python
StreamingAccumulator(
    capture_payload: bool
)
```

### `capture_payload=False`

只记录：

```text
model
usage
finish_reason
chunk_count
first meaningful token detected
```

不保存完整：

```text
content
reasoning
tool arguments
```

### `capture_payload=True`

才聚合完整 Payload。

判断：

```text
should_sample
AND
payload_strategy in ("masked", "full")
```

---

# 10. P1-NEW-05：文档状态必须真实反映完成度

当前文档写：

```text
P0/P1 fixes complete
```

不准确。

建议改为：

```text
Foundation Fix Phase 1 complete
Remaining blockers:
- API contract sync
- timing semantics
- trace-level filter semantics
- schema migration
```

不要在代码未闭环时标：

```text
complete
```

---

# 11. 本轮自动化测试要求

## 11.1 API Contract

Backend 返回真实 Metrics JSON。

Frontend Contract Test 验证：

```text
MetricsSummary
TimeSeriesPoint
ModelInfo
SpanRecord
```

字段一致。

## 11.2 Timing

覆盖：

```text
role-only first chunk
empty content chunk
first content token
reasoning token
tool_call token
non-streaming
```

## 11.3 Trace Status Filter

```text
Trace A: OK/ERROR mixed
Trace B: all OK
```

`status=OK` 只能返回 B。

## 11.4 Trace Duration Filter

验证：

```text
min_duration_ms
max_duration_ms
```

确实生效。

## 11.5 Model Filter

命中 model 后：

```text
完整 Trace
完整 span_count
完整 duration
```

不能被切残。

## 11.6 Migration

旧 DB：

```text
schema v1
```

启动新代码后：

```text
自动升级
旧数据保留
新数据可写
```

## 11.7 End-to-End UI

真实启动：

```text
Proxy
Core
UI
Mock LLM / One-API
```

验证：

```text
Dashboard
Trace List
Trace Detail
Pagination
Streaming
```

浏览器运行时无：

```text
undefined
NaN
字段缺失
```

---

# 12. 本轮实施顺序

## Phase A：必须先修

1. 前后端 API Contract
2. 时间指标语义
3. Trace Filter
4. SQLite Migration

## Phase B：数据一致性

5. TimeSeries Metrics
6. Pagination 扩展性

## Phase C：工程收尾

7. MASK_KEYS env
8. Streaming 内存模式
9. README / CLAUDE 状态同步
10. E2E Contract Tests

---

# 13. 本轮暂不处理

以下任务暂不作为本轮 blocker：

```text
OTLP Receiver
OpenTelemetry Collector
独立 Payload Store
Auth / RBAC
Reporter exponential backoff
Application SDK
LangChain Instrumentation
Agent Trace
Gateway Native Instrumentation
```

但代码修改不得阻碍这些未来能力。

---

# 14. Definition of Done

本轮结束后必须达到：

```text
Backend API
=
Frontend Type Contract
```

```text
Trace Count
LLM Call Count
Span Count
语义明确且一致
```

```text
Streaming:
first_chunk_ms ≠ ttft_ms ≠ duration_ms
```

```text
Non-streaming:
ttft_ms = null
first_chunk_ms = null
duration_ms = total latency
```

```text
Trace Filter:
status / duration = Trace-level semantics
model = Span exists semantics
返回完整 Trace
```

```text
旧 SQLite DB:
可平滑升级
不要求删库
```

```text
docker compose up
→ Proxy
→ Core
→ UI
→ 完整请求链
→ Dashboard / Trace Detail 正常
```

---

# 15. 完成本轮后再进入下一阶段

只有本轮 Foundation Fix 全部通过后，才开始：

```text
Application SDK
    ↓
业务任务创建 Root Trace

Agent
├── LLM
├── TOOL
├── LLM
└── TOOL
```

然后再做：

```text
Gateway Native Instrumentation
├── Routing
├── Channel
├── Retry
└── Provider
```

最终目标仍然是：

```text
Application SDK
Telemetry Proxy
One-API
LiteLLM
Gateway Native
        │
        ▼
统一 Trace / Span / OTLP
        │
        ▼
Observability Platform
```

本轮不要改变这一总体架构方向。
