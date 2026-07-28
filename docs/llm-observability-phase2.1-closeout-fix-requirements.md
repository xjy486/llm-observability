# LLM Observability Phase 2.1 Closeout 修复需求文档

> 适用仓库：`xjy486/llm-observability`  
> 审查范围：从 `9ef89c74ccd45e27fe7297b09c1430c3177a6de5` 起，到当前 Phase 2.1 最新实现  
> 当前状态：Application SDK / Agent Trace 主体架构已经成立，但 Public SDK 生命周期、Streaming Span 生命周期、Token 聚合等仍有关键正确性问题。  
> 本轮目标：**完成 Phase 2.1 Closeout，修完后正式冻结 Application SDK 基础，再进入 Phase 2.2 Tool Span。**

---

# 1. 总体结论

当前已经正确实现：

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

目标 Trace：

```text
Trace ABC
└── AGENT
    └── LLM
        └── GATEWAY
```

目前 4 个 P0 blocker：

```text
P0-1 Reporter 生命周期未由 Public SDK 自动管理
P0-2 OpenAI Instrumentor 生命周期错误
P0-3 Streaming LLM Span 生命周期错误
P0-4 Token 在 LLM + GATEWAY 间可能双计数
```

P1 收口项：

```text
nested trace 语义
sample_rate / api_key 实际生效
SDK/Proxy masking 一致
dedup 分支仍传播 traceparent
内部 ownership/metadata header 不向 Provider 泄露
```

---

# 2. P0-1：Reporter 生命周期必须由 Public API 自动管理

## 问题

当前：

```python
Observability.init(...)
```

只创建 `Reporter` 和 `Tracer`，没有启动 `Reporter.start()`。

而 Reporter 只有 `start()` 后才会创建：

```text
aiohttp.ClientSession
background flush task
```

`_flush()` 在 `_session is None` 时直接返回。

因此普通用户按文档调用：

```python
Observability.init(...)

with Observability.trace(...):
    client.chat.completions.create(...)

Observability.shutdown()
```

可能出现：

```text
AGENT / LLM 只进入本地 queue
Proxy 的 GATEWAY 正常入库
Core 中出现孤立 GATEWAY，parent 指向不存在的 LLM
```

当前 Real E2E 通过手动：

```python
await Observability._reporter.start()
```

绕过了这个问题。Public SDK 不允许要求用户访问 private 属性。

## 需求

`Observability.init()` 后 SDK 必须自动具备上报能力，并同时支持：

```text
普通同步 Python
asyncio 应用
```

推荐使用独立后台线程 + 独立 asyncio event loop 管理 Reporter。

生命周期：

```text
Observability.init()
→ 自动启动 Reporter

Observability.shutdown()
→ 停止接收
→ flush
→ stop
→ 关闭 session/task/thread
→ reset state
```

建议增加 `atexit` best-effort flush。

## 验收

只使用 Public API：

```python
Observability.init(...)

with Observability.trace(name="task"):
    client.chat.completions.create(...)

Observability.shutdown()
```

Core 必须得到：

```text
AGENT
└── LLM
    └── GATEWAY
```

禁止 E2E 再使用：

```text
Observability._reporter
Observability._tracer
```

---

# 3. P0-2：OpenAI Instrumentor 必须由同一个实例管理生命周期

## 问题

当前 init：

```python
OpenAIInstrumentor().instrument(...)
```

shutdown：

```python
OpenAIInstrumentor().uninstrument()
```

是两个不同实例。

而 `_patched` 是实例状态，可能导致：

```text
init：Instrumentor A patch 成功
shutdown：Instrumentor B 认为自己没 patch → 不恢复
再次 init：重复 patch
```

极端情况下 `_original_create` 可能指向 patched function，产生递归或双重采集。

## 需求

`Observability` 长期持有：

```python
_openai_instrumentor
```

生命周期：

```text
init
→ 创建一个实例
→ instrument()

shutdown
→ 同一个实例.uninstrument()
→ clear
```

必须支持：

```text
init → shutdown → init → shutdown
```

反复执行。

## 测试

新增回归：

```text
1. 保存 original create
2. init 后确认被 patch
3. shutdown 后恢复 original
4. 再次 init
5. 调一次 OpenAI
6. 只产生 1 个逻辑 LLM Span
7. shutdown 后再次恢复 original
```

---

# 4. P0-3：Streaming LLM Span 生命周期必须覆盖整个 Stream

## 问题

当前 patch：

```python
response = original_create(...)
return response
finally:
    span.end()
    report()
```

对于 `stream=True`，`create()` 返回 Stream 对象时模型输出还未完成。

当前实际变成：

```text
create(stream=True)
→ 返回 iterator
→ LLM Span 立即结束 ❌
→ 用户之后才 for chunk in stream
```

导致：

```text
LLM duration 错
最终 usage/output 缺失
stream 中途错误无法落到 LLM Span
```

## 需求

对 Streaming 返回对象增加包装器，例如：

```python
ObservedStream
```

要求保持 OpenAI SDK 原行为，同时在以下时机 finalize Span：

```text
iterator 正常耗尽
显式 close
用户提前结束并 close
stream exception
provider 中途断流
```

生命周期：

```text
创建 LLM Span
→ 发起请求并注入 traceparent
→ 返回 ObservedStream
→ 用户消费 Stream
→ 完成/关闭/异常
→ span.end()
→ report
```

Proxy GATEWAY 继续负责：

```text
HTTP status
first_chunk_ms
ttft_ms
wire-level duration
stream response aggregation
```

逻辑 LLM Span 不要和 GATEWAY 重复承担 wire timing。

## 验收

```python
with Observability.trace(...):
    stream = client.chat.completions.create(stream=True)
    for chunk in stream:
        ...
```

最终：

```text
AGENT duration >= LLM duration
LLM Span 结束时间 ≈ Stream 消费结束时间
```

Stream 迭代异常时：

```text
LLM status = ERROR
业务异常继续抛出
```

---

# 5. P0-4：Token Ownership 统一，禁止双计数

## 问题

同一次调用可能：

```text
LLM Span      total_tokens=30
GATEWAY Span  total_tokens=30
```

而 Trace Summary 对所有 Span：

```sql
SUM(total_tokens)
```

会得到：

```text
60 ❌
```

导致 Dashboard、Trace List、Trace Detail 口径不一致。

## 最终规则

冻结：

```text
产品级 Token Ownership = LLM Span
```

LLM Span：

```text
input_tokens
output_tokens
total_tokens
```

GATEWAY 可保留 provider observed usage 作为 attributes 调试信息，但不得参与产品级 Token 聚合。

## Storage 修复

所有 Trace / Dashboard / Cost 类产品统计：

```sql
CASE WHEN span_kind = 'LLM' THEN tokens ELSE 0 END
```

仅统计 LLM Span。

## 测试

构造：

```text
1 AGENT
1 LLM total_tokens=30
1 GATEWAY total_tokens=30
```

断言：

```text
Trace total_tokens = 30
LLM Call Count = 1
Span Count = 3
```

---

# 6. P1-1：修复 nested trace 跨 Trace 父子关系

## 问题

当前嵌套：

```python
with Observability.trace("outer"):
    with Observability.trace("inner"):
        ...
```

可能生成：

```text
Outer: Trace A / Span A1
Inner: Trace B / Span B1 / Parent=A1
```

即 Trace B 的 parent 属于 Trace A，非法。

## 需求

Phase 2.1 推荐直接禁止 nested `trace()`：

```text
已有 active trace 时再次调用 trace()
→ 抛明确错误
→ 提示后续使用 agent()/tool()/span() 创建 child span
```

不要静默生成跨 Trace parent。

---

# 7. P1-2：`sample_rate` 与 `api_key` 必须真实生效

## sample_rate

Root Trace 创建时做 Head Sampling：

```python
sampled = random.random() < sample_rate
```

Child Span 继承 sampled。

`sampled=False` 时：

```text
traceparent flags=00
不 enqueue SDK telemetry
```

## api_key

Reporter 请求支持：

```http
Authorization: Bearer <api_key>
```

即使 Core 暂时未强制 auth，Public API 参数也不能是假配置。

---

# 8. P1-3：SDK 与 Proxy Masking 规则保持一致

SDK 当前主要是 Key-based Masking，Proxy 已经有 Key + Regex。

必须统一 `masked` 语义，至少覆盖：

```text
api_key / authorization / token / secret / password
OpenAI-style sk-*
Bearer token
Cookie
文本中出现的 password/token/secret 模式
```

例如：

```text
content = "my key is sk-xxxx"
```

也必须被 masked。

长期建议抽公共 privacy module，本轮至少保证规则一致。

---

# 9. P1-4：Dedup 时不能停止 Context Propagation

## 问题

当前逻辑类似：

```python
if current_ctx.logical_llm_span_active:
    return original_create(...)
```

未来 LangChain 已创建逻辑 LLM Span 后，OpenAI Instrumentor 会跳过创建新 Span，这是对的；但如果同时不注入：

```text
traceparent
X-LLM-OBS-Span-Role: llm
```

Proxy 会丢失上下文或重新创建 LLM Span。

## 正确语义

Dedup 只表示：

```text
不再创建新的逻辑 LLM Span
```

仍必须：

```text
复用当前 LLM Context
注入 traceparent
注入 ownership marker
调用原始 OpenAI
```

为 Phase 2.3 LangChain 预留正确行为。

---

# 10. P1-5：内部 Observability Header 不得向 Provider 泄露

Proxy 消费以下内部 Header 后应 strip：

```text
X-LLM-OBS-Span-Role
X-Session-Id
X-User-Id
X-App-Name
X-Business-Scene
```

默认流程：

```text
SDK
→ Proxy 读取并转为 telemetry metadata
→ forwarding 时删除
→ One-API / Provider 不应收到
```

`traceparent` 例外：它需要由 Proxy 重新生成当前 Span context 后继续向下游传播。

---

# 11. 必须新增的自动化测试

## 11.1 Public API Reporter Lifecycle

不得访问 private reporter：

```python
Observability.init(...)
with Observability.trace(...):
    ...
Observability.shutdown()
```

断言 Core 收到 AGENT。

## 11.2 Public SDK → Proxy → Core E2E

禁止：

```python
await Observability._reporter.start()
```

最终必须：

```text
AGENT
└── LLM
    └── GATEWAY
```

## 11.3 Init / Shutdown / Re-init

验证：

```text
patch
restore
re-patch
无递归
无双重 LLM Span
```

## 11.4 Streaming Lifecycle

验证：

```text
Span 在完整 Stream 结束后才 finalize
stream error → LLM ERROR
```

## 11.5 Token Double Count

```text
LLM=30
GATEWAY=30
Trace total=30
```

## 11.6 Nested Trace

不得产生跨 Trace parent。

## 11.7 Sampling

```text
sample_rate=0 → 不入库 SDK Span
sample_rate=1 → 正常入库
```

## 11.8 Dedup Propagation

已有 logical LLM Span 时：

```text
不新增 LLM Span
但 request 仍带 traceparent + ownership marker
```

## 11.9 Internal Header Strip

最终 upstream request 不包含内部观测 Header。

---

# 12. 实施顺序

## Phase A：4 个 P0

```text
1. Reporter Lifecycle
2. Instrumentor Lifecycle
3. Streaming LLM Lifecycle
4. Token Ownership
```

## Phase B：语义收口

```text
5. Nested Trace
6. sample_rate / api_key
7. Masking parity
8. Dedup propagation
9. Internal header stripping
```

## Phase C：测试与文档

```text
10. Public API E2E
11. Streaming E2E
12. Re-init regression
13. Token accounting test
14. README / CLAUDE 状态同步
```

---

# 13. 本轮禁止事项

Closeout 完成前不要开始：

```text
Tool Span
LangChain Auto Instrumentation
AzureOpenAI
AsyncOpenAI
CrewAI
AutoGen
LlamaIndex
Gateway Native OTel
OTLP Collector
```

本轮唯一目标：

> 让 Phase 2.1 不只是“底层组件拼起来能跑”，而是“用户只使用 Public SDK API 就能稳定、正确地跑”。

---

# 14. Definition of Done

用户只需要：

```python
Observability.init(...)

with Observability.trace(...):
    client.chat.completions.create(...)

Observability.shutdown()
```

Core 必须稳定得到：

```text
Trace ABC
└── AGENT
    └── LLM
        └── GATEWAY
```

并满足：

```text
Reporter 自动管理生命周期
OpenAI patch 可安全 init/shutdown/re-init
Streaming LLM Span 覆盖完整 Stream
Token 不双计数
Nested Trace 不产生非法 parent
sample_rate / api_key 真正生效
SDK / Proxy privacy 语义一致
Dedup 仍传播 traceparent + ownership
内部观测 Header 不泄露到最终 Provider
```

完成后项目状态才可更新为：

```text
Phase 2.1 Application SDK & Agent Trace
✅ Complete
```

之后再进入：

```text
Phase 2.2 Tool Span
```
