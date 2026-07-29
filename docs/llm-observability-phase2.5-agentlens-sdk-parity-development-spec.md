# LLM Observability Phase 2.5 开发需求：AgentLens SDK Parity Closure

> 适用仓库：`xjy486/llm-observability`  
> 开发基线：`994a5c4423de064fdae4fd2d62bf543ae1f435b1`  
> 前置状态：Phase 2.1～2.4 已完成并冻结  
> 下一主阶段：Phase 3 Gateway Native Observability  
> 阶段定位：补齐 AgentLens 式 Application SDK 的核心接入体验，不继续向 LangGraph 深挖

---

## 0. 决策摘要

Phase 2.5 重新定义为：

```text
Phase 2.5 — AgentLens SDK Parity Closure
```

本阶段只解决一个问题：

> 当前系统已经具备 OpenAI、Tool、LangChain Agent/Runnable 观测内核，但普通用户仍需要理解多个高级 API。Phase 2.5 要把这些能力收敛成 AgentLens 式的低门槛 SDK 产品体验。

目标入口：

```python
from llm_observability import Observability
from llm_observability.decorators import agent, chain, task, tool, llm

Observability.init(
    app_name="demo.agent",
    endpoint="http://localhost:8001",
    auto_instrument_openai=True,
    auto_instrument_langchain=True,
)
```

高级 API `observe_agent()`、`observe_runnable()`、Middleware 和 Callback Handler 继续保留，但不再作为普通用户的首选接入方式。


---

## 1. 背景与目标

Phase 2.1～2.4 已完成：

```text
OpenAI 自动插桩
TOOL Span
LangChain create_agent
LangChain Runnable / Callback
sync / async / stream / astream
Retry、Context、Privacy、Fail-open
```

当前主要产品缺口：

```text
没有统一 @agent/@chain/@task/@llm
没有 annotate()
没有 Association Properties API
没有 message_id
没有面向用户的 Client/Server 分布式追踪助手
LangChain 仍以显式 wrapper/middleware 为主
没有 Instruments 和 block_instruments
```

本阶段目标：

```text
G1 统一手动装饰器
G2 初始化后自动采集 OpenAI/LangChain
G3 支持 annotate
G4 支持 user/session_id/message_id/business_scenario
G5 支持分布式 Client/Server Trace
G6 统一 Instrument 生命周期和禁用机制
```


---

## 2. 非目标

Phase 2.5 不做：

```text
LangGraph Node / Checkpoint / Interrupt / Resume
Replay / Fork / Workflow Thread
One-API Native Routing / Retry / Channel / Fallback
LiteLLM Native Adapter
Embedding、Qdrant、Bedrock 自动插桩
@embedding、@retrieval、@rerank
完整 APM、评测、告警、成本优化
```

后续路线：

```text
Phase 3  Gateway Native Observability
Phase 4  Embedding / Retrieval / Rerank 生态
Phase 5  LangGraph Advanced Workflow
```


---

## 3. Span 语义

保留：

```text
AGENT
LLM
TOOL
GATEWAY
```

新增：

```text
TASK
```

不新增独立 `CHAIN` SpanKind。映射如下：

| API | SpanKind | 关键属性 |
|---|---|---|
| `@agent` | `AGENT` | `operation.type=agent` |
| `@chain` | `TASK` | `task.type=chain` |
| `@task` | `TASK` | `task.type=task` |
| `@tool` | `TOOL` | `tool.type` |
| `@llm` | `LLM` | `gen_ai.operation.name` |
| Client helper | `TASK` | `task.type=client_call` |
| Server helper | `AGENT` | `operation.type=server_call` |

Phase 2.4 自动发现的 LCEL Chain 继续使用 Event；Phase 2.5 用户显式声明的 `@chain` 才创建 TASK Span。

### Agent Root 规则

无 Active Trace：

```text
@agent 创建 AGENT Root
```

从远端 Carrier 继承：

```text
继承 TraceID，创建 AGENT Server Span
```

已有本地 Active Trace：

```text
默认 nested_mode="error"
显式 nested_mode="reuse" 时复用当前 Trace，并添加 sdk.agent.reused Event
```

本阶段不创建嵌套 AGENT。


---

## 4. SDK 初始化与 Instruments

建议 API：

```python
Observability.init(
    app_name="demo.agent",
    endpoint="http://localhost:8001",
    api_key=None,
    payload_strategy="masked",
    sample_rate=1.0,
    auto_instrument_openai=True,
    auto_instrument_langchain=True,
    block_instruments=None,
    max_attribute_bytes=8 * 1024,
    max_payload_bytes=32 * 1024,
    fail_open=True,
)
```

新增：

```python
class Instruments(str, Enum):
    OPENAI = "openai"
    LANGCHAIN = "langchain"
```

示例：

```python
Observability.init(
    block_instruments={Instruments.LANGCHAIN},
)
```

规则：

```text
block_instruments 优先于 auto_instrument_*
instrument/ uninstrument 必须幂等、线程安全、可逆
重复 init/shutdown 不得重复 patch
单个 Instrument 失败不得影响业务和其他 Instrument
Observability 必须持有真实 Instrumentor 实例
```


---

## 5. LangChain 初始化后自动采集

目标：

```python
Observability.init(auto_instrument_langchain=True)

chain.invoke(...)
agent.invoke(...)
```

自动得到：

```text
AGENT
├── TOOL
└── LLM
    └── GATEWAY
```

用户无需显式调用：

```text
observe_agent
observe_runnable
langchain_middleware
```

自动模式与显式模式同时存在时必须去重：

```text
每个 Model Attempt 仅 1 LLM
每个 Tool Attempt 仅 1 TOOL
每个 Provider Attempt 仅 1 GATEWAY
```

正式编码前必须做 Compatibility Spike。实现优先级：

```text
1. LangChain 官方 Callback/Config 扩展点
2. 受控 Instrumentor 包装稳定公开入口
3. 最后才考虑有限且可逆的 Monkey Patch
```

禁止修改用户 CallbackManager、替换用户 Callback 或 Patch 任意 Runnable 实例。


---

## 6. 统一装饰器运行时

新增公共模块：

```text
llm_observability/decorators.py
```

公开：

```python
@agent()
@chain()
@task()
@tool()
@llm()
```

全部装饰器必须复用同一套：

```text
Decorator Runtime
Input Binder
Output Capture
Streaming Wrapper
Privacy Pipeline
Association Resolver
Context Cleanup
```

必须支持：

```text
同步函数
异步函数
同步 Generator
异步 Generator
实例方法、类方法、静态方法
```

Streaming Span 生命周期必须覆盖完整消费，并正确处理：

```text
close / aclose
GeneratorExit
CancelledError
消费者提前停止
业务异常
```

Telemetry 错误不得改变业务返回值或业务异常。仅配置错误允许抛出。

SDK 未初始化：

```text
fail_open=True  → warning + 业务照常
fail_open=False → RuntimeError
```


---

## 7. 装饰器要求

### `@agent`

```python
@agent()
def qa_agent(query: str):
    ...
```

自动捕获函数参数和返回值，生成 AGENT Root。普通异常标记 ERROR 并重新抛出；GeneratorExit、CancelledError 等控制流不标记普通 ERROR。

### `@chain`

```python
@chain()
def document_pipeline(document):
    ...
```

创建：

```text
TASK
task.type=chain
```

必须存在 Active Trace；无 Trace 时按 `fail_open` 处理，不自动创建 AGENT。

### `@task`

```python
@task()
def html_to_markdown(html):
    ...
```

创建：

```text
TASK
task.type=task
```

允许 TASK 嵌套 TASK、TOOL、LLM。

### `@tool`

必须复用 Phase 2.2 的 Tool Runtime，不得复制第二套 ToolContextManager。现有 `Observability.instrument_tool()` 继续保留并调用同一底层实现。

### `@llm`

```python
@llm()
def call_custom_model(model, messages):
    ...
```

创建逻辑 LLM。若函数内部调用 OpenAI SDK：

```text
@llm 创建 LLM
OpenAI Instrumentor 不再创建第二个 LLM
Provider 请求仍创建 GATEWAY
```

必须设置 `logical_llm_span_active=True`。


---

## 8. `Observability.annotate()`

API：

```python
Observability.annotate(
    span=None,
    input_data=None,
    output_data=None,
    attributes=None,
    tags=None,
    error=None,
)
```

`span=None` 使用当前 Active Span。

行为：

```text
input_data/output_data 覆盖自动捕获值
attributes 合并到 Span
tags 保存到 sdk.tags
error 设置状态和错误字段
```

禁止覆盖：

```text
trace_id
span_id
parent_span_id
span_kind
start_time
end_time
duration_ms
```

所有数据必须执行：

```text
Safe Serialize
Sensitive Key Masking
Pattern Masking
Size Guard
Reserved Key Protection
```

无 Active Span 时按 `fail_open` 处理。


---

## 9. Association Properties

规范字段：

```text
user
session_id
message_id
business_scenario
```

兼容别名：

```text
user_id → user
business_scene → business_scenario
```

API：

```python
token = Observability.set_association_properties({
    "user": "alice",
    "session_id": "session-1",
    "message_id": "message-1",
    "business_scenario": "customer-service",
})
try:
    ...
finally:
    Observability.reset_association_properties(token)
```

推荐：

```python
with Observability.association_context(
    user="alice",
    session_id="session-1",
    message_id="message-1",
    business_scenario="customer-service",
):
    ...
```

属性必须继承到：

```text
AGENT
TASK
TOOL
LLM
GATEWAY
```

优先级：

```text
Span 显式值
> Decorator 显式值
> Association Context
> Remote Carrier
> None
```

使用 ContextVar，异常、取消、Generator 退出后不得泄漏。所有值采用 Fail-closed Sanitization；Masking 失败返回 `<redacted>`。


---

## 10. 分布式 Trace API

新增：

```text
inject_carrier
extract_carrier
track_task_client_call
track_agent_server_call
```

默认传播：

```text
traceparent
baggage
```

为了兼容当前实现，可继续读取：

```text
X-Session-Id
X-User-Id
X-App-Name
X-Business-Scene
```

Carrier 只允许包含 Trace Context 和 Association Metadata，不得包含 Prompt、Response、API Key 或完整 Tool Output。

### Client

```python
headers = {}

with track_task_client_call("profile-service", carrier=headers) as span:
    response = requests.post(url, headers=headers, json=data)
    span.set_output(response.text)
```

创建：

```text
TASK
task.type=client_call
task.role=client
```

### Server

```python
with track_agent_server_call(
    "profile-handler",
    carrier=request.headers,
):
    ...
```

创建：

```text
AGENT
operation.type=server_call
span.role=server
```

合法远端 Context：

```text
继承 TraceID
Server AGENT.parent = Client TASK
```

非法或缺失 Context：

```text
创建新 Trace
```

Session/User 只能用于筛选，不能用于猜测 TraceID。


---

## 11. Core 与 UI 修改

### Core

新增 `TASK` SpanKind，并同步修改：

```text
Canonical Record Validation
Ingestion
Storage
Query API
Trace Summary
Test Fixtures
```

Canonical Record 新增：

```text
message_id
```

TASK 属性：

```text
task.name
task.type
task.call_id
task.role
```

Trace Summary 增加：

```text
task_count
chain_count
```

其中：

```text
chain_count = TASK 且 task.type=chain
```

### UI

Trace Tree 映射：

```text
TASK + task.type=chain       → CHAIN
TASK + task.type=task        → TASK
TASK + task.type=client_call → CLIENT CALL
```

Trace 列表新增 `message_id` 筛选。TASK Detail 展示 Input、Output、Duration、Status、Error、Attributes 和 Association Properties。


---

## 12. Privacy、Sampling 与资源生命周期

Sampling 只在 Root 决策一次，TASK/TOOL/LLM/GATEWAY 全部继承。分布式下游继承 `trace_flags`。

Unsampled：

```text
保留 Context
不做大 Payload 序列化
不上报 Record
继续传播 traceparent
```

Attribute 与 Payload 分开限制：

```text
max_attribute_bytes 默认 8 KiB，最大 128 KiB
max_payload_bytes 默认 32 KiB
```

截断必须记录：

```text
*.truncated=true
*.original_size_bytes
```

所有装饰器复用现有 Reporter，不得新增第二套 Reporter。正常、异常、close/aclose、span.end 失败都必须恢复 Context、注销 Event Sink 并释放本地 Span 引用。


---

## 13. 测试要求

### Decorator

```text
agent/chain/task/tool/llm 的 sync、async
generator、async generator
nested agent error/reuse
TASK Parent
@llm + OpenAI 去重
close/aclose/cancellation/context cleanup
```

### Annotate

```text
当前 Span 与显式 Span
覆盖 Input/Output
属性脱敏和保留键保护
无 Span fail-open
序列化失败
```

### Association

```text
全部 SpanKind 继承
nested scope
异常恢复
async task 隔离
Generator 生命周期
message_id 持久化
别名规范化
Masking fail-closed
```

### Instruments

```text
auto OpenAI
auto LangChain
block OpenAI/LangChain
重复 init 不重复 patch
shutdown 完整 uninstrument
单个 Instrument 失败隔离
自动与手动模式无重复 Span
```

### Distributed

```text
traceparent/baggage 注入与提取
非法 Trace Context
Client/Server 同 Trace
Server Parent 正确
Association 继承与本地覆盖
Carrier 不含敏感 Payload
```

### Fail-open

```text
未初始化
span.start/end 失败
Reporter 失败
业务异常不被替换
错误对象 __str__ 失败
Annotation 失败
```


---

## 14. Real E2E

### Scenario 1：纯手动 Agent

```text
@agent
└── @task
    └── @llm
        └── GATEWAY
```

断言：

```text
1 AGENT
1 TASK
1 LLM
1 GATEWAY
Parent 正确
```

### Scenario 2：初始化后 LangChain Auto

只调用：

```python
Observability.init(auto_instrument_langchain=True)
```

执行 LangChain Agent，断言 TOOL/LLM/GATEWAY 正确且无显式 wrapper。

### Scenario 3：`@llm + OpenAI Auto`

断言：

```text
1 LLM
1 GATEWAY
GATEWAY.parent=LLM
```

### Scenario 4：Association

所有 AGENT/TASK/TOOL/LLM/GATEWAY 具有相同 user、session_id、message_id、business_scenario。

### Scenario 5：跨服务

```text
Service A AGENT
└── TASK client_call
        │
        ▼
Service B AGENT server_call
└── LLM
    └── GATEWAY
```

断言相同 TraceID、Server Parent=Client、Association 一致。

### Scenario 6：Streaming

`@agent async generator → LangChain astream → OpenAI stream`，断言 Duration、TTFT、aclose 和 Context 清理正确。


---

## 15. 推荐实施顺序

```text
Step 0  冻结 TASK、Agent Nested、Association 优先级、Carrier 和 fail_open
Step 1  打通 TASK：SDK → Core → Storage → Query → UI
Step 2  实现统一 Decorator Engine：@agent/@chain/@task
Step 3  复用 @tool，并实现 @llm 与 OpenAI 去重
Step 4  实现 annotate
Step 5  实现 Association Properties 与 message_id
Step 6  实现 Distributed Client/Server Helpers
Step 7  实现 Instruments、block_instruments 和 auto LangChain
Step 8  Real E2E 与用户文档
Step 9  Phase 2.1～2.4 全量回归
```

预计新增：

```text
decorators.py
annotation.py
association.py
distributed.py
instruments.py
task.py
instrumentation/langchain.py
```

预计修改：

```text
spans.py
context.py
config.py
propagation.py
tracer.py
__init__.py
Core ingestion/storage/query
Web trace tree/filter/detail
```


---

## 16. 本阶段禁止事项

```text
把 LangGraph 高级工作流塞入本阶段
开始 One-API Native 修改
扩展 Qdrant/Bedrock
复制多个 Decorator Runtime
用 SessionID 猜 TraceID
用进程全局变量保存 Association
把 Prompt/Response 放入 Carrier
自动 Patch 任意用户 Runnable
创建重复 LLM/TOOL
新增第二套 Reporter
```


---

## 17. Definition of Done

完成必须满足：

```text
@agent/@chain/@task/@tool/@llm 可用
Observability.annotate 可用
Association Properties 和 message_id 可用
Distributed Client/Server Helper 可用
Instruments/block_instruments 可用
```

Trace 映射正确：

```text
@agent → AGENT
@chain → TASK(task.type=chain)
@task  → TASK(task.type=task)
@tool  → TOOL
@llm   → LLM
Provider → GATEWAY
```

执行模式正确：

```text
sync
async
generator
async generator
stream close
astream aclose
cancellation
```

自动插桩正确：

```text
OpenAI 初始化后自动采集
LangChain 初始化后自动采集
手动与自动模式无重复 Span
shutdown 完整恢复
```

关联与分布式正确：

```text
user/session_id/message_id/business_scenario 继承到全部 Span
Client TASK 与 Server AGENT 同 Trace
Parent 正确
非法 Carrier Fail-open
Carrier 无敏感 Payload
```

兼容性正确：

```text
Phase 2.1～2.4 全量回归通过
旧公共 API 保持可用
Core/UI 支持 TASK 与 message_id
```

全部满足后标记：

```text
Phase 2.5 AgentLens SDK Parity Closure
✅ COMPLETE
✅ FROZEN
```


---

## 18. 下一阶段交接

Phase 2.5 冻结后，立即进入：

```text
Phase 3 — Gateway Native Observability
```

Phase 3 聚焦：

```text
One-API / LiteLLM 内部
Routing
Channel Selection
Provider Attempt
Retry
Fallback
Quota / Rate Limit
实际 Model Mapping
```

Phase 2.5 的结束标准不是继续支持更多框架，而是：

> Application SDK 已具备 AgentLens 式核心接入体验，可以停止继续纵向扩展 LangChain，回到产品最初的 Gateway 深度观测主线。
