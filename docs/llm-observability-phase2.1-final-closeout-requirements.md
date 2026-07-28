# LLM Observability Phase 2.1 Final Closeout 修复需求文档

> 适用仓库：`xjy486/llm-observability`  
> 基线提交：`a4e8e164cc2d40858989aaaa355eff63d8f97af5`  
> 当前状态：Phase 2.1 主体架构已经完成，Public SDK 基本可用，但仍存在少量会影响 Trace 正确性、采样完整性和兼容性的收尾问题。  
> 本轮目标：**完成 Phase 2.1 最后一轮正确性 Closeout。完成后正式冻结 Phase 2.1，进入 Phase 2.2 Tool Span。**

---

# 1. 当前已完成能力

当前系统已经正确具备：

```text
Application SDK
    ↓
AGENT Span
    ↓
LLM Span
    ↓ traceparent + ownership marker
Telemetry Proxy
    ↓
GATEWAY Span
```

理想 Trace：

```text
Trace ABC
└── AGENT
    └── LLM
        └── GATEWAY
```

已完成并不再要求重做：

```text
✅ Observability.init()/shutdown() 自动管理 Reporter
✅ Reporter 使用后台线程 + 独立 asyncio loop
✅ OpenAI Instrumentor 使用单实例生命周期
✅ init → shutdown → init 可恢复 patch
✅ Streaming 使用 ObservedStream 延迟 Span finalize
✅ Token 产品聚合仅统计 LLM Span
✅ Nested trace() 已禁止
✅ api_key 已接入 Reporter
✅ Dedup 时仍传播 traceparent + ownership marker
✅ Internal Observability Headers 已在 Proxy strip
✅ Key + Regex Masking 已有基础实现
```

本轮不要重新设计这些能力。

---

# 2. 本轮剩余问题总览

需要修复 6 项：

```text
P0-1 Streaming ContextVar 生命周期错误
P0-2 Sampling 未贯穿完整 Trace

P1-1 Reporter shutdown 未 drain 全部 queue
P1-2 无 SDK Trace Summary metadata 回归
P1-3 Session/User Metrics Filter 语义错误
P1-4 SDK / Proxy Masking Key 集仍不一致
```

其中：

```text
P0-1 / P0-2
```

是正式冻结 Phase 2.1 前必须修复的 blocker。

---

# 3. P0-1：Streaming Span 生命周期与 ContextVar 生命周期必须解耦

## 3.1 当前问题

当前 OpenAI Instrumentation 创建逻辑 LLM Span 后：

```python
token = set_context(llm_ctx)
```

Streaming 时返回：

```python
ObservedStream(
    response,
    span,
    tracer,
    token,
)
```

但不会立即：

```python
reset_context(token)
```

而是等 Stream 最终：

```text
StopIteration
close()
exception
```

才在：

```python
ObservedStream._finalize()
```

中 reset。

因此：

```text
AGENT Context
    ↓
create(stream=True)
    ↓
set current Context = LLM
    ↓
返回 Stream
    ↓
用户开始消费前
current Context 仍然是 LLM ❌
```

这里把：

```text
Span 生命周期
```

和：

```text
ContextVar 激活生命周期
```

错误绑定在了一起。

---

# 4. P0-1 可能导致的错误

## 场景 A：Stream 未消费前发起第二次 LLM

```python
with Observability.trace("task"):

    stream1 = client.chat.completions.create(
        model="gpt-4",
        stream=True,
        messages=[...],
    )

    response2 = client.chat.completions.create(
        model="gpt-4",
        messages=[...],
    )

    for chunk in stream1:
        ...
```

当前第二次请求可能看到：

```text
current_ctx.logical_llm_span_active = True
```

导致 Dedup：

```text
不创建 LLM #2
直接复用 LLM #1 Context
```

最终错误结构：

```text
AGENT
└── LLM #1
    ├── GATEWAY #1
    └── GATEWAY #2   ❌
```

正确结构：

```text
AGENT
├── LLM #1
│   └── GATEWAY #1
└── LLM #2
    └── GATEWAY #2
```

---

## 场景 B：用户提前 break

```python
stream = client.chat.completions.create(stream=True)

for chunk in stream:
    break
```

普通 Python iterator 在 `break` 时不会自动调用自定义：

```python
close()
```

因此可能：

```text
ObservedStream 永不 finalize
LLM Span 永不 report
ContextVar 永不 reset
logical_llm_span_active 永久残留
```

---

## 场景 C：Stream 逃出 Trace Scope

```python
with Observability.trace("task"):
    stream = client.chat.completions.create(stream=True)

for chunk in stream:
    ...
```

如果 ContextVar 的 reset 延迟到 Stream 完成：

```text
AGENT context exit
和
LLM context reset
```

顺序可能错乱，导致恢复已结束的 Context。

---

# 5. P0-1 最终设计要求

必须明确：

> **Context 激活生命周期仅用于创建/传播 Child Span；Span 本身的生命周期可以更长。**

正确流程：

```text
AGENT Context active
        ↓
创建 LLM Span / LLM Context
        ↓
临时 set_context(LLM)
        ↓
注入 traceparent + ownership
        ↓
调用 OpenAI create()
        ↓
拿到 response / stream
        ↓
立即 restore AGENT Context   ← 必须
        ↓
ObservedStream 自己持有：
  span
  tracer
  lifecycle state
        ↓
Stream 完成
        ↓
只 finalize Span
不再操作外部 ContextVar token
```

即：

```text
Context lifetime
≠
Span lifetime
```

---

# 6. P0-1 推荐实现

对于 Streaming：

```python
token = set_context(llm_ctx)

try:
    response = original_create(...)
finally:
    reset_context(token)
```

如果：

```text
stream=False
```

可继续立即 finalize LLM Span。

如果：

```text
stream=True
```

则：

```text
1. create LLM span
2. set LLM context
3. inject headers
4. original_create()
5. restore parent context
6. return ObservedStream(span=llm_span)
```

`ObservedStream` 不再持有 ContextVar token。

---

# 7. P0-1 Stream 未完全消费时的行为

必须处理：

```text
正常 exhaustion
close()
context manager exit
iteration error
对象被遗弃 / early break
```

建议至少：

### 正常结束

```text
StopIteration
→ finalize OK
```

### 显式 close

```text
close()
→ close underlying stream
→ finalize
```

### Stream error

```text
→ finalize ERROR
→ re-raise
```

### Early break

Python `break` 不会自动触发 close。

至少需要：

```text
文档要求调用 close()
+
支持 context manager
```

更稳妥可增加：

```python
__del__()
```

进行 best-effort finalize，但不可依赖 `__del__` 作为主要逻辑。

---

# 8. P0-1 必须新增测试

## Case 1：Stream 未消费前第二次 LLM

```text
AGENT
├── LLM #1 streaming
└── LLM #2 non-streaming
```

断言：

```text
LLM Span Count = 2
两者 parent 都是 AGENT
两个 GATEWAY 分别 parent 对应各自 LLM
```

## Case 2：Stream 创建后 Context 已恢复

```python
with Observability.trace(...):
    agent_ctx = get_current_context()

    stream = client.chat.completions.create(stream=True)

    current = get_current_context()

    assert current.span_id == agent_ctx.span_id
```

## Case 3：Stream exception

```text
LLM Span ERROR
Context 恢复为 AGENT
```

---

# 9. P0-2：Sampling 必须贯穿完整 Trace

## 9.1 当前问题

Root Trace：

```python
sampled = random.random() < sample_rate
```

AGENT：

```text
sampled=False
→ 不 report
```

这个已经正确。

但 Child LLM Context 虽然继承：

```python
sampled=current_ctx.sampled
```

LLM finalize 时仍然：

```python
reporter.report(...)
```

没有判断 sampled。

Proxy 又会继承：

```text
traceparent flags=00
```

但仍会上报 GATEWAY。

因此：

```text
sample_rate=0
```

可能得到：

```text
AGENT      ×
LLM        ✓
GATEWAY    ✓
```

形成孤儿 Span。

---

# 10. P0-2 最终 Sampling 规则

Head Sampling 一旦在 Root Trace 决定：

```text
sampled=True / False
```

整个 Trace 必须继承。

## sampled=True

```text
AGENT      ✓
LLM        ✓
GATEWAY    ✓
```

## sampled=False

```text
AGENT      ×
LLM        ×
GATEWAY    ×
```

仍然允许传播：

```text
traceparent
flags=00
```

但不能入库。

---

# 11. P0-2 SDK 修复

LLM Span finalize：

```python
if span_context.sampled:
    reporter.report(...)
```

Streaming 与 Non-streaming 都一致。

建议不要从 `Span` 自己猜 sampled。

ObservedStream 创建时显式保存：

```text
sampled
```

或者 Span 自身记录：

```text
sampled
```

确保 finalize 时可判断。

---

# 12. P0-2 Proxy 修复

Proxy 收到：

```text
traceparent flags=00
```

必须：

```text
继续请求转发
继续 traceparent 传播
但不 report telemetry
```

注意：

```text
sampled=False
```

不是业务失败，也不能影响 LLM 请求。

---

# 13. P0-2 验收测试

## Case A

```text
sample_rate=0

SDK
→ OpenAI
→ Proxy
→ Provider
```

业务请求正常。

Core：

```text
0 AGENT
0 LLM
0 GATEWAY
```

## Case B

```text
sample_rate=1
```

Core：

```text
AGENT
└── LLM
    └── GATEWAY
```

## Case C

断言：

```text
traceparent flags=00
```

从：

```text
SDK
→ Proxy
→ downstream
```

一直保持未采样语义。

---

# 14. P1-1：Reporter shutdown 必须 drain 全部 Queue

## 当前问题

Reporter：

```python
stop()
```

只调用一次：

```python
await _flush()
```

而 `_flush()` 一次最多发送：

```text
batch_size
```

默认：

```text
50
```

假设 shutdown：

```text
queue = 120
```

当前可能：

```text
发送 50
关闭 session/thread
剩余 70 丢失
```

---

# 15. P1-1 需求

Shutdown：

```text
flush remaining queue
```

必须按 batch 持续 drain。

示意：

```python
deadline = now + shutdown_timeout

while queue and now < deadline:
    await flush_one_batch()
```

若 Core 不可用：

```text
不得无限卡住 shutdown
```

超时后：

```text
记录 dropped_count
日志 warning
结束
```

---

# 16. P1-1 测试

```text
batch_size=10
queue=25
shutdown
```

Mock Core 成功。

断言：

```text
sent_count=25
queue=0
```

失败场景：

```text
Core unavailable
```

断言 shutdown 在限定时间内返回。

---

# 17. P1-2：无 SDK 模式 Trace Summary Metadata 必须兼容

## 当前问题

为了 SDK Trace 优先使用 AGENT metadata，Summary SQL 改成：

```sql
MAX(
  CASE WHEN span_kind='AGENT'
  THEN session_id
  END
)
```

但无 SDK：

```text
Trace
└── LLM
```

不存在 AGENT。

因此可能：

```text
session_id = NULL
user_id = NULL
app_name = NULL
business_scene = NULL
```

即使 LLM Span 本身有这些字段。

---

# 18. P1-2 正确规则

Trace-level metadata：

```text
优先 AGENT / Root Span
如果没有 AGENT
→ fallback 到任意 non-null Span metadata
```

SQL 建议：

```sql
COALESCE(
    MAX(CASE WHEN span_kind='AGENT' THEN session_id END),
    MAX(session_id)
)
```

其余：

```text
user_id
app_name
business_scene
```

同理。

---

# 19. P1-2 测试

## SDK Trace

```text
AGENT metadata=S1/U1/App1
LLM metadata=null
```

Summary：

```text
S1/U1/App1
```

## No-SDK Trace

```text
LLM metadata=S2/U2/App2
```

Summary：

```text
S2/U2/App2
```

不能回归。

---

# 20. P1-3：Session/User Metrics Filter 必须按 Trace 语义

## 当前问题

SDK：

```text
AGENT
session_id=S1

LLM
session_id=NULL
```

当前 Metrics：

```sql
WHERE session_id='S1'
AND span_kind='LLM'
```

结果：

```text
trace_count = 1
llm_call_count = 0 ❌
tokens = 0 ❌
```

---

# 21. P1-3 最终语义

`session_id` / `user_id` 是 Trace-level Filter。

必须：

```text
Phase 1:
找 candidate TraceIDs

条件：
Trace 内存在 session_id=S1
或优先 Root/AGENT metadata=S1

Phase 2:
对 candidate TraceIDs 的所有 LLM Span 聚合
```

类似：

```sql
WITH candidate_traces AS (
    SELECT DISTINCT trace_id
    FROM spans
    WHERE session_id = ?
)

SELECT ...
FROM spans
WHERE trace_id IN (
    SELECT trace_id FROM candidate_traces
)
AND span_kind='LLM'
```

---

# 22. P1-3 验收

Trace：

```text
AGENT session=S1
├── LLM #1
└── LLM #2
```

查询：

```text
metrics?session_id=S1
```

必须：

```text
trace_count = 1
llm_call_count = 2
tokens = 两个 LLM 总和
```

---

# 23. P1-4：SDK / Proxy Masking Key 集必须真正统一

## 当前问题

SDK：

```text
api_key
apikey
authorization
token
secret
password
passwd
credential
cookie
set-cookie
```

Proxy 还有：

```text
api-key
x-api-key
x-auth-token
access_token
refresh_token
private_key
secret_key
```

例如：

```json
{
  "access_token": "abc123"
}
```

SDK 当前可能不脱敏。

---

# 24. P1-4 要求

统一敏感 Key：

```text
authorization
api_key
apikey
api-key
x-api-key
x-auth-token
token
access_token
refresh_token
secret
secret_key
private_key
password
passwd
credential
cookie
set-cookie
proxy-authorization
```

统一 Regex：

```text
sk-*
Bearer *
password/passwd
token
secret
api_key/api-key
authorization
```

建议新增共享定义：

```text
common privacy constants
```

如果当前工程结构不方便共享包，至少：

```text
SDK tests
Proxy tests
```

都对同一 golden sensitive cases 运行。

---

# 25. P1-4 测试

至少：

```json
{
  "access_token": "abc",
  "refresh_token": "def",
  "private_key": "ghi",
  "secret_key": "jkl",
  "x-api-key": "mno"
}
```

全部：

```text
REDACTED
```

文本：

```text
my key is sk-...
Authorization: Bearer ...
token=...
password=...
```

全部脱敏。

---

# 26. 额外建议：Streaming Usage 处理

这项不是 blocker，但建议本轮顺手处理。

当前产品 Token Ownership 已冻结：

```text
LLM Span
```

但 Streaming Token 只有在 Provider 返回 usage chunk 时才能获取。

建议：

```text
若 stream=True
且调用方未显式设置 stream_options
```

可考虑自动增加：

```python
stream_options={"include_usage": True}
```

但必须确认目标 OpenAI-compatible Provider 是否兼容。

若不能自动注入，则：

```text
README 明确说明：
Streaming token usage 依赖 Provider/include_usage 支持
```

不要让用户误以为 Streaming Token 一定完整。

---

# 27. 自动化测试要求汇总

必须新增：

```text
1. Streaming create 后 Context 立即恢复
2. 两个交错 LLM：stream 未消费 + 第二个 LLM
3. Sampling=0 E2E：AGENT/LLM/GATEWAY 全部不入库
4. Reporter shutdown drain > batch_size
5. No-SDK metadata fallback
6. Session/User filter 能统计 Child LLM
7. 完整 Masking key golden cases
```

保留原有：

```text
init/shutdown/re-init
stream exhaustion/error/close
token no double count
nested trace
dedup propagation
internal header strip
```

---

# 28. 推荐实施顺序

## Phase A：Blocker

```text
1. Streaming ContextVar / Span 生命周期解耦
2. Sampling 全链路继承
```

## Phase B：Correctness

```text
3. Reporter shutdown drain
4. No-SDK metadata fallback
5. Session/User Metrics trace-level filter
6. Masking parity
```

## Phase C：验证

```text
7. 新增 regression tests
8. Real E2E
9. 更新 CLAUDE.md / README status
```

---

# 29. 本轮禁止事项

在这轮完成前，不要开发：

```text
Tool Span
LangChain
AsyncOpenAI
AzureOpenAI
CrewAI
AutoGen
LlamaIndex
Gateway Native OTel
OTLP Collector
```

本轮不允许再次扩大范围。

---

# 30. Definition of Done

完成后必须满足：

```text
Public SDK:
Observability.init()
→ 自动可用

Observability.shutdown()
→ 全部待发送 Span best-effort drain
```

```text
Streaming:
Context 在 create() 后立即恢复 Parent
Span 生命周期持续到 Stream 完成
```

```text
Sampling:
sample_rate=0
→ AGENT 0
→ LLM 0
→ GATEWAY 0
```

```text
Token:
只统计逻辑 LLM Span
无 double count
```

```text
Metadata:
SDK Trace 优先 AGENT
No-SDK Trace fallback LLM/other Span
```

```text
Metrics Filter:
session/user 作为 Trace-level filter
仍能聚合 Child LLM
```

```text
Privacy:
SDK / Proxy sensitive keys + regex 语义一致
```

全部新增 Regression + Real E2E 通过后，项目状态可正式更新为：

```text
Phase 2.1 Application SDK & Agent Trace
✅ COMPLETE / FROZEN
```

随后进入：

```text
Phase 2.2 Tool Span

Trace
└── AGENT
    ├── LLM
    │   └── GATEWAY
    ├── TOOL
    ├── LLM
    │   └── GATEWAY
    └── TOOL
```
