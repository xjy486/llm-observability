# LLM Observability Phase 2 开发规格：Application SDK & Agent Trace

> 仓库：`xjy486/llm-observability`  
> 基线状态：Foundation Fix 已完成并冻结，基于提交 `3be77634237408570cd9a4498bccb0386d27f384`  
> 文档类型：开发规格 / Technical Development Spec  
> 本阶段目标：从“单次 LLM 请求级可观测”升级为“Agent 业务任务级 Trace 可观测”  
> 目标版本：Phase 2.1 → Phase 2.3

---

# 1. 背景

当前系统已经具备稳定的请求级可观测能力：

```text
Client
  ↓
Telemetry Proxy
  ↓
One-API / LiteLLM
  ↓
LLM Provider
```

并已支持：

```text
Trace Context 继承/创建
Streaming / Non-streaming
duration_ms
first_chunk_ms
ttft_ms
Token
Prompt / Response
Trace / LLM / Span Metrics
Trace Filtering
Pagination
SQLite Migration
Frontend / Backend Contract
```

当前模式下：

```text
没有上游业务 Trace
        ↓
一次 LLM Request
        =
一个自动创建的 Trace
```

该模式适合普通 LLM API 调用，但不足以表达 Agent 场景。

真实 Agent 任务通常包含：

```text
用户发起任务
    ↓
Agent 推理
    ↓
LLM
    ↓
Tool
    ↓
LLM
    ↓
Tool
    ↓
LLM
    ↓
任务完成
```

理想可观测结构应为：

```text
Trace：一次业务任务

└── AGENT
    ├── LLM
    ├── TOOL
    ├── LLM
    ├── TOOL
    └── LLM
```

因此 Phase 2 的核心不是继续扩展 Proxy，而是新增：

```text
Application SDK
+
Agent Trace Context
+
业务级 Span 语义
```

---

# 2. 本阶段目标

Phase 2 必须实现：

1. Application SDK 可以在业务进程中创建一次业务任务级 Trace；
2. SDK 能创建 AGENT / LLM / TOOL 等 Span；
3. SDK 创建的 Trace Context 能通过 OpenAI Client → Telemetry Proxy 向下游传播；
4. Proxy 不再把已有业务 Trace 切断；
5. 一个 Agent Task 中多次 LLM / Tool 调用可以出现在同一个 Trace 中；
6. 不同采集层之间不得产生重复逻辑 LLM Span；
7. SDK Telemetry 上报失败不得影响业务逻辑；
8. 现有无 SDK 模式必须继续工作：无上游 Trace 时仍然“单请求 Trace 兜底”。

---

# 3. 非目标

本阶段明确不做：

```text
LiteLLM Native OTel
One-API Native Instrumentation
Gateway Retry / Routing / Channel Span
OpenTelemetry Collector 完整接入
独立 Payload Store
告警中心
成本优化
Prompt Evaluation
AI 自动诊断
多租户 RBAC
完整 LangChain / CrewAI / AutoGen / LlamaIndex 全框架适配
```

Phase 2 重点只解决：

> Application SDK 如何建立业务 Trace，并和现有 Proxy / Core 串成一条完整链路。

---

# 4. 总体架构

目标架构：

```text
Application / Agent
│
├── Application SDK
│   ├── Trace Context
│   ├── Agent Span
│   ├── LLM Instrumentation
│   ├── Tool Span
│   └── Async Reporter
│
▼
OpenAI / AzureOpenAI SDK
│
│ traceparent
▼
Telemetry Proxy
│
│ traceparent
▼
One-API / LiteLLM
│
▼
LLM Provider

Application SDK ───────┐
Telemetry Proxy ────────┼──► Observability Core
Future Gateway Native ──┘
```

最终同一个 Trace 应形成：

```text
Trace ABC

└── agent.run
    ├── llm.completion
    │   └── proxy.request
    ├── tool.call
    ├── llm.completion
    │   └── proxy.request
    └── tool.call
```

---

# 5. 核心 Trace 语义

## 5.1 Trace

一个 Trace 表示：

> 一次完整业务任务 / Agent Run。

例如：

```text
“帮我分析这个仓库并修改 bug”
```

整个执行过程：

```text
LLM
Tool
LLM
Tool
LLM
```

必须属于同一个 Trace。

## 5.2 Session 与 Trace

必须继续保持：

```text
User
└── Session
    ├── Trace A
    ├── Trace B
    └── Trace C
```

禁止：

```text
Session = Trace
```

Session 仅用于关联多个业务任务。

## 5.3 无业务 Trace 时的兜底规则

必须保留现有规则：

```text
如果请求没有上游 Trace Context
    ↓
Telemetry Proxy 自动创建 Trace
    ↓
单次 LLM Request = 1 Trace
```

即：

```text
业务层有 Trace
→ 继承业务 Trace

业务层无 Trace
→ Proxy 单请求 Trace 兜底
```

该行为不可通过配置手动切换，应由 Trace Context 自然决定。

---

# 6. Span 类型定义

本阶段冻结以下 Span 类型。

## 6.1 AGENT

表示一次 Agent / Workflow / Business Task 执行。

示例：

```text
agent.run
```

属性建议：

```text
span_kind = AGENT
agent.name
agent.type
business.scene
session_id
user_id
app_name
```

## 6.2 LLM

表示一次逻辑 LLM 调用。

示例：

```text
llm.completion
```

属性：

```text
gen_ai.request.model
gen_ai.response.model
gen_ai.operation.name
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
duration_ms
ttft_ms
first_chunk_ms
stream
```

## 6.3 TOOL

表示 Agent 调用一次外部工具。

例如：

```text
terminal
web.search
database.query
file.read
calculator
```

Span 名称建议：

```text
tool.<tool_name>
```

属性：

```text
tool.name
tool.type
tool.input
tool.output
tool.status
```

Payload 必须遵守现有 Mask / Full / Metadata 策略。

## 6.4 INTERNAL

表示 Agent 内部步骤、Workflow Node 或业务内部操作。

Phase 2.1 不要求自动采集 INTERNAL Span。

---

# 7. Span Ownership：避免重复 LLM Span

这是本阶段最重要的设计约束之一。

未来同一次 LLM 请求可能同时被：

```text
Application SDK
OpenAI Instrumentor
Telemetry Proxy
Gateway Native
```

观察到。

不得产生：

```text
AGENT
├── LLM
└── LLM
```

这种重复逻辑调用。

## 7.1 Ownership 原则

Application SDK 负责：

```text
业务语义
Agent
Tool
逻辑 LLM Call
```

Telemetry Proxy 负责：

```text
网络 / Gateway 边界
HTTP/SSE
实际 Latency / First Chunk / TTFT
Provider Response
```

因此同一次调用建议表示为：

```text
AGENT
└── LLM
    └── proxy.request
```

而不是：

```text
AGENT
├── LLM from SDK
└── LLM from Proxy
```

## 7.2 Proxy Span 类型调整

当存在上游逻辑 LLM Span时：

```text
Proxy Span
span_kind = GATEWAY
span_name = proxy.request
```

当没有上游业务 Trace / LLM Span 时：

```text
Proxy 仍可创建 Root LLM Span
```

即：

```text
无 SDK：

Trace
└── llm.completion

有 SDK：

Trace
└── agent.run
    └── llm.completion
        └── proxy.request
```

## 7.3 如何判断是否已有逻辑 LLM Span

不能仅凭“存在 traceparent”判断已有 LLM Span，因为 Agent Root Span 直接发请求时也可能只有 AGENT Context。

建议 SDK 除注入 `traceparent` 外，再注入明确 ownership 标记。

优先推荐标准 `baggage`：

```text
baggage: llm.obs.logical_span=llm
```

如实现成本过高，可先使用内部 Header：

```text
X-LLM-OBS-Span-Role: llm
```

Proxy 收到后：

```text
已有逻辑 LLM Span
→ 当前 Span 创建为 GATEWAY / proxy.request

没有逻辑 LLM 标记
→ 当前 Span 创建为 LLM fallback
```

该标记只负责 Span Ownership，不负责 Trace 分组；Trace 分组仍以 `traceparent` 为准。

---

# 8. Python SDK 包结构

建议新增：

```text
sdk/python/
├── pyproject.toml
└── llm_observability/
    ├── __init__.py
    ├── config.py
    ├── context.py
    ├── tracer.py
    ├── spans.py
    ├── reporter.py
    ├── propagation.py
    ├── instrumentation/
    │   ├── base.py
    │   └── openai.py
    └── utils/
        ├── ids.py
        └── masking.py
```

---

# 9. SDK 初始化 API

Phase 2.1 建议 API：

```python
from llm_observability import Observability

Observability.init(
    app_name="agent_server",
    endpoint="http://localhost:8001",
    api_key=None,
)
```

支持配置：

```python
Observability.init(
    app_name="agent_server",
    endpoint="http://localhost:8001",
    api_key="...",
    payload_strategy="masked",
    sample_rate=1.0,
    auto_instrument_openai=True,
)
```

## 9.1 Init 行为

调用 `Observability.init(...)` 应完成：

```text
1. 初始化全局 Config
2. 初始化 Async Reporter
3. 初始化 Context Provider
4. 注册 OpenAI Instrumentor
5. 注册 shutdown / flush hook
```

必须幂等：重复调用不得重复 patch。

---

# 10. 手动 Trace API

Phase 2.1 必须优先提供手动 API。

推荐：

```python
with Observability.trace(
    name="fix_login_task",
    session_id="session-123",
    user_id="user-456",
    business_scene="coding_agent",
):
    ...
```

内部创建：

```text
Trace ABC
└── agent.run
```

## 10.1 Context Manager 语义

进入时：

```text
生成 trace_id
生成 root span_id
写入 ContextVar
记录 start_time
```

退出时：

```text
记录 end_time
计算 duration
设置 status
异步上报 Root Span
恢复父 Context
```

异常时：

```text
status = ERROR
error_type
error_message
异常继续向外抛出
```

SDK 不得吞业务异常。

---

# 11. Agent Span API

推荐：

```python
with Observability.agent(name="code_agent"):
    ...
```

若当前没有 Trace：自动创建 Trace + AGENT Root Span。

若已有 Trace：创建 Child AGENT Span。

Phase 2.1 可简化为 `trace()` 默认创建 AGENT Span，不要求同时暴露复杂 `agent()` API。

---

# 12. Tool Span API

Phase 2.2 新增：

```python
with Observability.tool(
    name="terminal",
    input={"command": "git status"},
):
    result = run_command(...)
```

结果：

```text
AGENT
├── LLM
├── TOOL terminal
└── LLM
```

推荐支持：

```python
with Observability.tool(name="search") as span:
    result = search(...)
    span.set_output(result)
```

Decorator 可放后续 P1。

---

# 13. OpenAI 自动插桩

Phase 2.1 只要求：

```text
OpenAI Python SDK
/v1/chat/completions
```

后续再扩：

```text
responses
embeddings
AzureOpenAI
```

## 13.1 用户体验

用户代码：

```python
from llm_observability import Observability
from openai import OpenAI

Observability.init(
    app_name="demo-agent",
    endpoint="http://localhost:8001",
)

client = OpenAI(
    base_url="http://localhost:8082/v1",
    api_key="..."
)

with Observability.trace(name="demo-task"):
    client.chat.completions.create(
        model="gpt-4",
        messages=[...]
    )
```

用户不需要替换 OpenAI Client，也不需要修改 `create()` 调用方式。

---

# 14. OpenAI Instrumentor 行为

调用 `client.chat.completions.create(...)` 前：

```text
读取当前 Trace Context
创建逻辑 LLM Span
生成 child span_id
激活 LLM Span Context
注入 traceparent
注入 ownership marker
调用原始 OpenAI SDK
```

调用完成后：

```text
记录结果
结束 LLM Span
恢复父 Context
```

必须同时覆盖同步与异步客户端；若 Phase 2.1 暂不支持 AsyncOpenAI，应在文档和运行时明确标记 unsupported，不得静默错误插桩。

---

# 15. Trace Context 传播

完整链路：

```text
Trace ABC

AGENT Span A
    │
    ▼
OpenAI Instrumentor

创建 LLM Span L

traceparent:
00-{ABC}-{L}-01

    │
    ▼
Telemetry Proxy

读取：
TraceID = ABC
Parent = L

创建：
proxy.request Span P

    │
    ▼
下游：
traceparent:
00-{ABC}-{P}-01
```

最终：

```text
Trace ABC

A: AGENT
└── L: LLM
    └── P: GATEWAY / proxy.request
```

---

# 16. Context 实现

Python SDK 必须使用：

```python
contextvars.ContextVar
```

不得只使用普通 global variable 或 thread-local。

Context 内容建议：

```python
@dataclass
class SpanContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    span_kind: str
    sampled: bool
    logical_llm_span_active: bool = False
```

---

# 17. Async / 并发传播要求

以下必须正确：

```python
with Observability.trace(...):
    await llm_call()
```

以及：

```python
await asyncio.gather(
    llm_call_1(),
    llm_call_2(),
)
```

两个并行 LLM Span：

```text
AGENT
├── LLM A
└── LLM B
```

不得变成：

```text
AGENT
└── LLM A
    └── LLM B
```

必须增加并发测试。

---

# 18. Reporter

SDK Reporter 原则与 Proxy 一致：

```text
Fail-open
Async
Batch
Bounded Queue
Telemetry failure != Business failure
```

```text
span.end()
    ↓
enqueue
    ↓
background batch
    ↓
Core / Ingestion
```

SDK 与 Proxy 必须共享 Canonical Telemetry Record，禁止 SDK 定义另一套 Trace Schema。

统一结构：

```json
{
  "trace_id": "...",
  "span_id": "...",
  "parent_span_id": "...",
  "span_name": "...",
  "span_kind": "...",
  "start_time": 0,
  "end_time": 0,
  "duration_ms": 0,
  "status": "OK",
  "attributes": {},
  "events": []
}
```

---

# 19. Span 去重 / Suppression

未来可能出现：

```text
LangChain Instrumentor
→ OpenAI Instrumentor
```

如果 LangChain 已创建逻辑 LLM Span，OpenAI Instrumentor 不能再创建第二个逻辑 LLM Span。

Phase 2.1 必须预留 Context 标记：

```python
current_context.logical_llm_span_active
```

逻辑：

```text
如果没有 logical_llm_span_active
→ OpenAI Instrumentor 创建 LLM Span

如果已有 logical_llm_span_active
→ 不创建新的逻辑 LLM Span
→ 只传播当前 Context
```

Phase 2.3 LangChain 实现必须复用这一机制。

---

# 20. Payload 采集

SDK 不重新发明隐私策略，沿用：

```text
off
metadata_only
masked
full
```

## 20.1 SDK 与 Proxy Payload Ownership

建议：

SDK 负责：

```text
逻辑 LLM Input
Agent Context
Tool Input/Output
```

Proxy 负责：

```text
实际 wire request / response
HTTP Metadata
Streaming Timing
```

Phase 2.1 可以允许逻辑层和 Gateway 层都保存必要元数据，但不应无意义复制同一完整 Payload 两份。

---

# 21. SDK Fail-open

所有 SDK Instrumentation 都必须保证：

```text
Observability Core down
≠
OpenAI 调用失败
```

```text
Telemetry serialization error
≠
Agent Task crash
```

业务异常仍正常抛出；只有观测逻辑异常应被隔离。

---

# 22. Error Span 语义

业务调用失败：

```text
LLM exception
Tool exception
Agent exception
```

对应 Span：

```text
status = ERROR
error_type
error_message
```

异常必须继续抛给业务调用方。

例如：

```python
with Observability.tool(...):
    raise RuntimeError("git failed")
```

结果：

```text
TOOL Span = ERROR
Trace = ERROR
```

---

# 23. Trace 状态计算

继续沿用 Core 规则：

```text
Trace 中任意 Span ERROR
→ Trace ERROR

否则
→ Trace OK
```

SDK 不单独维护 Trace Summary，Core 统一聚合。

---

# 24. Phase 2.1 MVP 范围

必须严格控制。

## P0

### SDK Core

```text
Observability.init
Trace Context
ContextVar
Span lifecycle
Async Reporter
W3C traceparent
```

### Manual Trace

```text
with Observability.trace(...)
```

### OpenAI Instrumentation

```text
chat.completions
非流式
Streaming
```

### Proxy Cooperation

```text
识别 SDK logical LLM Span
创建 GATEWAY proxy.request Span
```

### Core / UI

```text
展示：
AGENT
LLM
GATEWAY
```

## P1

```text
Tool Span Context Manager
Decorator
AzureOpenAI
AsyncOpenAI
```

## P2

```text
LangChain
Agent Framework Auto Instrumentation
```

---

# 25. Phase 2.1 端到端目标

最小演示：

```python
Observability.init(...)

with Observability.trace(
    name="demo-agent-task",
    session_id="session-1",
    user_id="user-1",
):
    client.chat.completions.create(...)
```

最终 UI：

```text
Trace demo-agent-task

AGENT                         5.4s
└── LLM                      5.2s
    └── proxy.request        5.1s
```

显示：

```text
TraceID
SessionID
UserID
App
Duration

LLM:
Model
Input
Output
Tokens

Proxy:
HTTP Status
duration
first_chunk
ttft
```

---

# 26. Phase 2.2 端到端目标

代码：

```python
with Observability.trace(name="research-task"):

    result = client.chat.completions.create(...)

    with Observability.tool(
        name="web_search",
        input={"query": "..."}
    ) as tool:
        result = search(...)
        tool.set_output(result)

    client.chat.completions.create(...)
```

UI：

```text
Trace research-task

AGENT
├── LLM
│   └── proxy.request
├── TOOL web_search
└── LLM
    └── proxy.request
```

---

# 27. Core 改造要求

当前 Core 已支持多 Span Trace。

本阶段需确认：

```text
AGENT
TOOL
GATEWAY
```

均可完成：

```text
ingest
storage
query
display
```

禁止 Core 假设“每个 Trace 只有 LLM Span”。

## 27.1 Metrics

继续保持：

```text
Trace Count
LLM Call Count
Span Count
```

`GATEWAY proxy.request` 不得计入 `LLM Call Count`。

LLM Call Count 仅统计：

```text
span_kind = LLM
```

---

# 28. UI 改造要求

Trace Detail 必须能展示：

```text
AGENT
├── LLM
│   └── GATEWAY
├── TOOL
└── LLM
```

建议明确 icon / label：

```text
AGENT
LLM
TOOL
GATEWAY
```

## 28.1 Span Detail

AGENT 显示：

```text
name
duration
status
session
user
business_scene
```

LLM 显示：

```text
model
prompt
response
tokens
```

TOOL 显示：

```text
tool name
input
output
status
duration
```

GATEWAY 显示：

```text
gateway
HTTP status
duration
first_chunk
ttft
```

---

# 29. 自动化测试

## 29.1 Trace Context

```text
手动 Trace
→ OpenAI LLM
→ Proxy
```

断言：

```text
TraceID 相同

AGENT span
  parent=None

LLM span
  parent=AGENT

Proxy span
  parent=LLM
```

## 29.2 无 SDK 兼容

```text
普通 Client
→ Proxy
```

仍然：

```text
1 LLM Request
=
1 Root Trace
```

不得破坏现有行为。

## 29.3 多 LLM

```text
Agent
├── LLM1
└── LLM2
```

断言：

```text
同一 TraceID
不同 SpanID
Parent 都是 Agent
```

## 29.4 并发

```python
await asyncio.gather(
    llm1(),
    llm2(),
)
```

结果必须是：

```text
AGENT
├── LLM1
└── LLM2
```

## 29.5 Exception

```text
LLM ERROR
```

断言：

```text
LLM status ERROR
Trace status ERROR
业务异常继续抛出
```

## 29.6 Reporter Failure

模拟 Core down：

```text
SDK Reporter 请求失败
```

业务 OpenAI 调用仍成功。

## 29.7 Duplicate LLM

断言：

```text
一次 OpenAI 调用
=
一个逻辑 LLM Span
```

Proxy 只能创建 GATEWAY Span，不得出现两个 LLM Span。

---

# 30. E2E 验收场景

## Scenario A：普通 LLM Client

```text
Client
→ Proxy
→ One-API
```

结果：

```text
Trace
└── LLM
```

保持现有行为。

## Scenario B：SDK + 单次 LLM

```text
SDK Trace
→ OpenAI
→ Proxy
```

结果：

```text
Trace
└── AGENT
    └── LLM
        └── GATEWAY
```

## Scenario C：Agent + Tool + 多 LLM

```text
Trace
└── AGENT
    ├── LLM
    │   └── GATEWAY
    ├── TOOL
    ├── LLM
    │   └── GATEWAY
    └── TOOL
```

---

# 31. Definition of Done

Phase 2.1 完成必须满足：

```text
Observability.init 可用
```

```text
业务可创建一次 Agent Task Trace
```

```text
OpenAI 调用自动成为 LLM Child Span
```

```text
Proxy 继续同一 Trace
```

```text
Proxy 不制造重复逻辑 LLM Span
```

```text
Trace Detail 正确展示 AGENT → LLM → GATEWAY
```

```text
无 SDK 模式仍保持单请求 Trace 兜底
```

```text
async / concurrency Context 不串线
```

```text
Observability 故障不影响业务请求
```

```text
所有新增行为有自动化测试
```

---

# 32. 推荐开发顺序

## Step 1：SDK Core

```text
Config
ContextVar
Span
Trace IDs
Reporter
```

## Step 2：Manual Trace API

先实现：

```python
with Observability.trace(...):
```

## Step 3：OpenAI Instrumentation

只支持：

```text
chat.completions
```

## Step 4：Trace Context Injection

确保：

```text
SDK → Proxy
```

父子关系正确。

## Step 5：Span Ownership

Proxy：

```text
有 logical LLM marker
→ GATEWAY

无 marker
→ LLM fallback
```

## Step 6：Core / UI

展示：

```text
AGENT
LLM
GATEWAY
```

## Step 7：E2E

验证：

```text
AGENT
└── LLM
    └── GATEWAY
```

## Step 8：进入 Phase 2.2

```text
TOOL Span
```

---

# 33. 本阶段禁止事项

在 Phase 2.1 E2E 完成前，不要：

```text
同时适配多个 Agent Framework
引入复杂 OTel Collector 架构
开发 Gateway Native Instrumentation
实现复杂 Span Links
开发 AI 自动诊断
重构现有 Storage 为大型分布式存储
```

本阶段唯一目标：

> 先让“一次业务任务 = 一个 Trace，LLM / Tool = Child Span”稳定跑通。

---

# 34. 最终目标形态

Phase 2 完成后：

```text
用户业务任务
        ↓
Application SDK

Trace ABC
└── AGENT
    ├── LLM
    │   └── GATEWAY
    ├── TOOL
    ├── LLM
    │   └── GATEWAY
    └── TOOL
```

未来 Phase 3 再向下扩展：

```text
AGENT
└── LLM
    └── GATEWAY
        ├── AUTH
        ├── ROUTING
        ├── PROVIDER #1 ERROR
        └── PROVIDER #2 OK
```

整个系统始终保持：

```text
Application SDK
Telemetry Proxy
One-API
LiteLLM
Gateway Native

        ↓

统一 Trace / Span 语义

        ↓

Observability Platform
```

该原则不得改变。
