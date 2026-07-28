# LLM Observability Phase 2.3 开发规格：LangChain 自动插桩

> 适用仓库：`xjy486/llm-observability`  
> 开发基线：`f6a924937b7ae84798d0ca1c2ed091ede061d82f`  
> 前置状态：Phase 2.1 Application SDK & Agent Trace、Phase 2.2 Tool Span 已完成并冻结  
> 文档类型：Technical Development Spec  
> 本阶段目标：针对 LangChain v1 `create_agent` 建立第一套 Framework Auto Instrumentation，将框架中的 Agent、Model、Tool 生命周期稳定映射到现有 `AGENT / LLM / TOOL / GATEWAY` Span 语义。

---

# 1. 背景

当前平台已经支持：

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

但 LangChain 用户通常只会：

```python
agent = create_agent(
    model=model,
    tools=[search, calculator],
)

result = agent.invoke({
    "messages": [...]
})
```

用户不应该手工包裹每次模型调用和每个工具执行。

Phase 2.3 的任务是：

> 在不改变 LangChain 业务逻辑的前提下，自动把 LangChain Agent Runtime 映射为平台已经冻结的 Span 模型。

---

# 2. LangChain 版本边界

Phase 2.3 首期仅支持：

```text
LangChain Python v1
langchain.agents.create_agent
```

## 2.1 冻结开发基线

Phase 2.3 的开发、代码审查和 Real E2E 统一使用以下精确版本：

```text
Python            3.10 / 3.12
langchain         1.3.14
langchain-core    1.5.1
langgraph         1.2.9
langchain-openai  1.4.0   # 仅 OpenAI-compatible Real E2E
```

禁止开发过程中直接使用未记录的浮动 `latest` 环境。

建议建立：

```text
requirements/phase2.3-langchain-lock.txt
```

内容：

```text
langchain==1.3.14
langchain-core==1.5.1
langgraph==1.2.9
langchain-openai==1.4.0
```

## 2.2 最低支持版本与发布范围

SDK 对外声明的最低支持版本：

```text
langchain >= 1.3.14, < 2.0
langchain-core >= 1.5.1, < 2.0
langgraph >= 1.2.9, < 1.3
Python >= 3.10, < 4.0
```

其中：

```text
1.3.14 / 1.5.1 / 1.2.9
```

是首个经过完整单测和 Real E2E 验证的版本组合。

更高的 LangChain 1.x 版本只有在兼容性 CI 通过后，才视为正式支持。

## 2.3 版本兼容性依据

`langchain==1.3.14` 的官方依赖范围允许：

```text
langchain-core >= 1.4.9, < 2.0
langgraph >= 1.2.5, < 1.3
```

本项目选择更高的最低基线：

```text
langchain-core >= 1.5.1
langgraph >= 1.2.9
```

以冻结本阶段实际开发环境，避免在较旧 Minor/Patch 上重复适配。

`langchain-openai==1.4.0` 要求：

```text
langchain-core >= 1.5.0, < 2.0
```

因此与本阶段的 `langchain-core==1.5.1` 兼容。

LangChain v1 的标准 Agent 入口是：

```python
from langchain.agents import create_agent
```

其底层运行于 LangGraph，并提供：

```text
before_agent
before_model
after_model
after_agent
wrap_model_call
wrap_tool_call
```

等 Middleware 生命周期。

---

# 3. 本阶段范围

必须支持：

```text
create_agent()
agent.invoke()
agent.ainvoke()
agent.stream()
agent.astream()
```

并自动采集：

```text
Agent Task
Model Call
Tool Call
Error
Retry Attempt
Parallel Tool
Framework Metadata
```

---

# 4. 非目标

本阶段明确不做：

```text
langchain-classic 旧 AgentExecutor
任意 LCEL Runnable 全量自动插桩
任意 LangGraph StateGraph 节点自动插桩
Retriever 独立 Span
Embedding Span
Vector Store Span
Prompt Template Span
Output Parser Span
Memory Span
Checkpointer Span
LangGraph Server 分布式 Trace
Sub-agent 专属 AGENT Child Span
CrewAI / AutoGen / LlamaIndex
MCP 自动插桩
跨进程 Context 传播
线程池自动 Context 传播
框架调用图全量可视化
LangChain Streaming TTFT 精确采集
```

---

# 5. 核心设计决策

Phase 2.3 使用：

```text
Observed Agent Wrapper
+
LangChain AgentMiddleware
```

结构：

```text
ObservedLangChainAgent
├── 创建和结束 AGENT Root Trace
│
└── LangChainObservabilityMiddleware
    ├── wrap_model_call  → LLM Span
    └── wrap_tool_call   → TOOL Span
```

不使用全局 Monkey Patch，也不在本阶段以 Callback Handler 为主实现。

原因：

```text
Monkey Patch 版本脆弱、难卸载、易与其他 SDK 冲突
Callback 会产生大量 Chain/Runnable 噪声，且 Context 生命周期更复杂
Middleware 是 LangChain v1 create_agent 的官方扩展点
```

Callback Handler 留给后续 Generic Runnable / LCEL 阶段。

---

# 6. 最终用户 API

推荐使用：

```python
from langchain.agents import create_agent
from llm_observability import Observability
from llm_observability.integrations.langchain import (
    LangChainObservabilityMiddleware,
    observe_agent,
)

Observability.init(
    app_name="research-service",
    endpoint="http://localhost:8001",
    payload_strategy="masked",
)

obs_middleware = LangChainObservabilityMiddleware()

agent = create_agent(
    model=model,
    tools=[web_search, calculator],
    middleware=[obs_middleware],
)

agent = observe_agent(
    agent,
    name="research-agent",
)

result = agent.invoke({
    "messages": [
        {"role": "user", "content": "Research AI observability"}
    ]
})
```

可同时提供便捷入口：

```python
middleware = Observability.langchain_middleware()

agent = Observability.instrument_langchain_agent(
    agent,
    name="research-agent",
)
```

但底层必须复用 `integrations/langchain` 中的唯一实现，禁止在 `Observability` 类中复制第二套生命周期。

---

# 7. 包结构建议

新增：

```text
sdk/python/llm_observability/integrations/
├── __init__.py
└── langchain/
    ├── __init__.py
    ├── middleware.py
    ├── agent_wrapper.py
    ├── llm_span.py
    ├── metadata.py
    └── compat.py
```

职责：

```text
middleware.py   → Model / Tool 生命周期
agent_wrapper.py→ Agent invoke/stream 生命周期
llm_span.py     → Framework Logical LLM Span
metadata.py     → LangChain 对象信息提取与清洗
compat.py       → 版本检查和可选导入
```

---

# 8. Optional Dependency

LangChain 不能成为核心 SDK 的硬依赖。

`pyproject.toml` 新增：

```toml
[project.optional-dependencies]
langchain = [
    "langchain>=1.3.14,<2.0",
    "langchain-core>=1.5.1,<2.0",
    "langgraph>=1.2.9,<1.3",
]
```

`langchain-openai` 只用于 OpenAI-compatible Real E2E，不作为 Provider-neutral Integration 的硬依赖。开发锁文件中固定：

```text
langchain-openai==1.4.0
```

安装：

```bash
pip install "llm-observability-sdk[langchain]"
```

核心 SDK 在未安装 LangChain 时必须继续正常导入。

Integration 缺依赖时给出明确错误：

```text
LangChain integration requires optional dependency.
Install with:

pip install "llm-observability-sdk[langchain]"
```

---

# 9. Trace 目标结构

标准 Agent Loop：

```text
Trace
└── AGENT research-agent
    ├── LLM
    │   └── GATEWAY
    ├── TOOL web_search
    ├── LLM
    │   └── GATEWAY
    └── TOOL calculator
```

Tool 内部调用模型：

```text
Trace
└── AGENT research-agent
    └── TOOL retrieval
        └── LLM
            └── GATEWAY
```

Parallel Tool：

```text
Trace
└── AGENT research-agent
    ├── TOOL search_a
    ├── TOOL search_b
    └── TOOL search_c
```

Parent 只由调用发生时的当前 Context 决定，禁止根据 `run_id`、`tool_call_id` 或 `AIMessage.tool_calls` 强行修改 Parent。

---

# 10. Agent Root Trace

`ObservedLangChainAgent` 负责：

```text
一次 invoke / ainvoke / stream / astream
=
一次业务 Trace
```

Root Span：

```text
span_kind = AGENT
span_name = agent.<name>
```

默认名称：

```text
langchain.agent
```

用户指定名称时：

```text
agent.research-agent
```

---

# 11. 已有 Active Trace 时的规则

```python
with Observability.trace("http-request"):
    observed_agent.invoke(...)
```

Wrapper 不得再次创建 Root Trace。

正确行为：

```text
检测到 Active Context
→ 不创建第二个 AGENT
→ 直接执行 Agent
→ Model / Tool 挂到当前 Context
```

建议支持：

```python
observe_agent(
    agent,
    name="research-agent",
    root_mode="auto",
)
```

模式：

```text
auto             无 Context 时创建 AGENT；有 Context 时复用
create           必须无 Context，否则报错
require_existing 必须已有 Context
```

默认 `root_mode="auto"`。

---

# 12. Agent Metadata

Wrapper 支持：

```python
observe_agent(
    agent,
    name="research-agent",
    session_id=...,
    user_id=...,
    business_scene=...,
)
```

参数允许固定字符串或 Callable。

默认映射：

```text
config.configurable.thread_id → session_id
config.run_name               → langchain.run_name
config.tags                   → langchain.tags
config.metadata               → langchain.metadata
```

优先级：

```text
显式参数
→ LangChain Config
→ None
```

所有值必须经过现有 safe serialization、Masking 和 Size Guard。

---

# 13. Agent Wrapper：invoke / ainvoke

同步：

```python
def invoke(self, input, config=None, **kwargs):
    with agent_scope(...):
        return self._agent.invoke(input, config=config, **kwargs)
```

异步：

```python
async def ainvoke(self, input, config=None, **kwargs):
    with agent_scope(...):
        return await self._agent.ainvoke(input, config=config, **kwargs)
```

要求：

```text
异常自动标记 AGENT ERROR
异常继续抛出
返回值不改变
Context 在 finally 中恢复
并发 ainvoke 互不污染
```

---

# 14. Agent Wrapper：stream / astream

错误实现：

```python
with trace:
    return agent.stream(...)
```

这会提前结束 Trace。

正确实现：

```python
def stream(...):
    def iterator():
        with agent_scope(...):
            try:
                yield from self._agent.stream(...)
            finally:
                ...
    return iterator()
```

Async 使用 Async Generator：

```python
async def astream(...):
    with agent_scope(...):
        try:
            async for item in self._agent.astream(...):
                yield item
        finally:
            ...
```

Trace 必须在以下情况结束：

```text
正常耗尽
异常
Generator close/aclose
调用方提前 break
任务取消
```

`ObservedLangChainAgent` 应通过 `__getattr__` 透明委托其他属性，但 Phase 2.3 只承诺 `invoke/ainvoke/stream/astream` 自动创建 Trace。

---

# 15. LangChain Middleware

新增：

```python
class LangChainObservabilityMiddleware(AgentMiddleware):
    ...
```

必须实现：

```text
wrap_model_call
awrap_model_call
wrap_tool_call
awrap_tool_call
```

Middleware 不负责创建 Root Trace。

若没有 Active Context：

```text
直接执行 handler
记录 debug warning
不创建孤立 LLM/TOOL
```

---

# 16. Model Call → LLM Span

同步：

```python
def wrap_model_call(self, request, handler):
    with logical_llm_span(request) as span:
        response = handler(request)
        span.set_response(response)
        return response
```

Async 同理。

创建 Context：

```python
SpanContext(
    trace_id=current.trace_id,
    span_id=new_span_id,
    parent_span_id=current.span_id,
    span_kind=SpanKind.LLM,
    sampled=current.sampled,
    logical_llm_span_active=True,
)
```

`logical_llm_span_active=True` 是与现有 OpenAI Instrumentor 去重的关键。

---

# 17. OpenAI LLM 去重

调用链：

```text
LangChain Middleware
→ OpenAI Python SDK Instrumentor
→ Proxy
```

正确结构：

```text
AGENT
└── LLM  ← LangChain Middleware 创建
    └── GATEWAY
```

OpenAI Instrumentor 检测到 `logical_llm_span_active=True` 后必须：

```text
不创建第二个 LLM
继续注入 traceparent
继续注入 logical ownership marker
```

禁止：

```text
AGENT
└── LLM LangChain
    └── LLM OpenAI
        └── GATEWAY
```

必须增加真实去重回归测试。

---

# 18. Provider-neutral LLM Span

LangChain Middleware 创建的 LLM Span 必须支持：

```text
OpenAI
Anthropic
Google
AWS Bedrock
自定义 BaseChatModel
```

不能把主流程绑定到具体 Provider 类型。

推荐属性：

```text
gen_ai.operation.name = chat
gen_ai.request.model
gen_ai.provider.name
framework.name = langchain
framework.version
langchain.component = model
langchain.model.class
langchain.run_name
langchain.tags
langchain.thread_id
langchain.attempt
```

缺失字段直接省略，不填大量 `unknown`。

模型名称提取顺序：

```text
request.model.model_name
request.model.model
request.model._llm_type
request.model.__class__.__name__
```

---

# 19. LLM Input / Output / Token

Input 优先读取 `request.messages`，规范化为普通 JSON：

```json
[
  {"type": "human", "content": "..."},
  {"type": "ai", "content": "...", "tool_calls": []}
]
```

禁止把 LangChain Message 对象原样塞进 Attributes。

Response 安全提取：

```text
ModelResponse
AIMessage
response.result
response.structured_response
```

Token Usage 优先级：

```text
AIMessage.usage_metadata
response_metadata.token_usage
LLMResult.llm_output.token_usage
```

映射：

```text
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
gen_ai.usage.total_tokens
```

缺失时不推测。

Phase 2.3 必须保证总 Duration、最终 Status 和框架提供时的最终 Token Usage；不强制保证 TTFT、first_chunk 和逐 Token Payload。

---

# 20. Tool Call → TOOL Span

`wrap_tool_call` 从 Request 提取：

```text
request.tool_call.name
request.tool_call.args
request.tool_call.id
```

复用 Phase 2.2 API：

```python
with tracer.tool(
    name=tool_name,
    tool_type="langchain",
    input=tool_args,
    call_id=tool_call_id,
    attributes=framework_attributes,
) as tool_span:
    result = handler(request)
    tool_span.set_output(result)
    return result
```

Async 同理。

Tool Name 提取顺序：

```text
request.tool_call["name"]
request.tool.name
request.tool.__class__.__name__
```

无法获取时使用 `langchain_tool`，并记录 `langchain.tool.name_missing=true`。

Tool Output 可能是：

```text
str / dict / ToolMessage / Command / Artifact / 自定义对象
```

全部复用 Phase 2.2 的 Safe Serialization、Masking 和 Size Guard。

---

# 21. Error 和 Interrupt 语义

Model/Tool Handler 抛业务异常：

```text
对应 Span status=ERROR
记录 error_type/error_message
异常继续抛出
```

若 LangChain Middleware 将异常转换成正常 `ToolMessage`，SDK 不自动猜测 ERROR，除非存在明确 `status="error"`。

LangGraph Interrupt 属于控制流，不一定是系统失败。对已知中断类型建议：

```text
status=OK 或 UNSET
langchain.interrupted=true
```

不要把等待人工审批默认标成系统 ERROR。

---

# 22. Retry 语义

目标：

```text
一次真实 Model/Tool Attempt
=
一个 LLM/TOOL Span
```

例如：

```text
AGENT
├── LLM attempt=1 ERROR
├── LLM attempt=2 OK
└── TOOL
```

Observability Middleware 应位于 Retry/Fallback Middleware 的内层。具体 Middleware 顺序必须通过支持版本的真实测试确认，不能仅凭猜测写死。

若 Runtime 提供 `execution_info.attempt_number`，记录 `langchain.attempt`；不存在时不自行推测。

---

# 23. Parallel Tool 和 Tool 内 LLM

Parallel Tool 必须形成兄弟 Span：

```text
AGENT
├── TOOL A
├── TOOL B
└── TOOL C
```

要求：

```text
每个 Tool 独立 SpanID
Parent 都是 AGENT
Tool 之间互不成为 Parent
ContextVar 不串线
```

Tool 内调用 Model：

```text
AGENT
└── TOOL retrieval
    └── LLM
        └── GATEWAY
```

必须使用真实 LangChain Tool 验证。

---

# 24. Nested Agent

Phase 2.3 不创建 Child AGENT Span。

若 Tool 内调用另一个 Observed Agent：

```text
检测到已有 Active Context
→ Nested Wrapper 不创建新 Root
→ 内部 LLM/TOOL 挂到当前 TOOL
```

结果：

```text
AGENT
└── TOOL subagent
    ├── LLM
    └── TOOL
```

Child AGENT 留作后续能力。

---

# 25. Sampling

Root Trace 只做一次采样决策。

后续：

```text
LLM
TOOL
GATEWAY
```

全部继承 `current.sampled`，禁止 Middleware 再次随机采样。

`sampled=False` 时：

```text
仍维持 Context 传播
不处理大 Payload
不 Reporter enqueue
不产生孤立 Span
```

回归断言：

```text
AGENT=0
LLM=0
TOOL=0
GATEWAY=0
```

---

# 26. Privacy 和 Metadata 限制

LangChain Integration 禁止建立第三套 Masking 规则。

以下内容统一复用现有能力：

```text
Agent Input/Output
Model Messages
Model Response
Tool Args
Tool Output
LangChain Metadata
Tags
Error Message
```

建议限制：

```text
langchain.tags 最大 50 个
单个 tag 最大 128 字符
langchain.metadata 最大 16 KiB
run_name 最大 128 字符
thread_id 最大 256 字符
```

超限写入 Truncated Marker。

---

# 27. Fail-open 和 Context 恢复

以下失败不得影响 Agent：

```text
Metadata 提取失败
Payload 序列化失败
Reporter 失败
未知 LangChain Minor Version
Integration 属性处理失败
```

正确行为：

```text
记录 warning/error
调用原始 handler
返回原始结果
```

配置错误可以明确报错：

```text
LangChain 未安装
root_mode 非法
传入对象不支持 invoke/ainvoke
```

Model、Tool、Agent 生命周期都必须在 `finally` 中恢复 Context，即使 `str(error)`、Response Serialization 或 Reporter 再次失败，也不能泄漏 Context。

---

# 28. 手动 SDK 兼容

支持：

```python
with Observability.trace("request"):
    agent.invoke(...)
```

Middleware 自动创建 LLM/TOOL。

支持：

```python
with Observability.trace("task"):
    with Observability.tool("subtask"):
        observed_agent.invoke(...)
```

内部 Span 挂到 TOOL。

以下用法可能产生重复 TOOL：

```python
@Observability.instrument_tool(name="search")
@tool
def search(...):
    ...
```

同时启用 LangChain Tool Middleware 时，本阶段不实现通用 TOOL 去重。文档必须明确：

> 由 LangChain 管理的 Tool 不要再手工添加 `Observability.instrument_tool()`。

---

# 29. Core / UI / Metrics

Phase 2.3 不新增 SpanKind，不新增数据库表。

继续使用：

```text
AGENT / LLM / TOOL / GATEWAY
```

Framework 信息进入 Attributes：

```text
framework.name = langchain
framework.version
langchain.component
langchain.run_name
langchain.tags
langchain.thread_id
langchain.attempt
langchain.model.class
langchain.tool.call_id
```

UI Span Detail 建议展示：

```text
Framework
Component
Run Name
Thread ID
Attempt
Tool Call ID
Tags
```

Dashboard 不新增独立 LangChain 指标，直接进入现有：

```text
trace_count
llm_call_count
tool_call_count
tool_error_rate
LLM latency
Tool latency
tokens
```

---

# 30. Version Compatibility Layer

`compat.py` 统一负责：

```text
LangChain version detection
AgentMiddleware imports
ModelRequest / ModelResponse imports
ToolCallRequest imports
Command / ToolMessage imports
GraphInterrupt imports
```

禁止在主业务文件散落大量 Import Fallback。

CI 至少覆盖：

```text
Python 3.10 + LangChain minimum 1.x
Python 3.12 + LangChain latest 1.x
```

---

# 31. 单元测试：Agent Wrapper

必须新增：

```text
test_observed_agent_invoke_creates_agent_trace
test_observed_agent_ainvoke_creates_agent_trace
test_observed_agent_error_marks_agent_error
test_observed_agent_reuses_existing_trace
test_observed_agent_root_mode_create_rejects_nested
test_observed_agent_stream_lifetime
test_observed_agent_stream_early_close
test_observed_agent_astream_lifetime
test_observed_agent_astream_cancel_restores_context
```

---

# 32. 单元测试：Model Middleware

```text
test_model_call_creates_llm_child
test_async_model_call_creates_llm_child
test_model_error_marks_llm_error
test_model_response_usage_metadata
test_model_payload_strategy_off
test_model_payload_strategy_masked
test_model_unsampled_skips_payload
test_model_context_restored
test_no_active_context_model_hook_is_noop
```

---

# 33. 单元测试：OpenAI 去重

必须验证：

```text
LangChain Middleware 创建一个 LLM
OpenAI Instrumentor 不创建第二个 LLM
OpenAI Instrumentor 仍注入 traceparent
Proxy 创建一个 GATEWAY
```

断言：

```text
LLM count = 1
GATEWAY count = 1
GATEWAY.parent_span_id = LLM.span_id
```

---

# 34. 单元测试：Tool Middleware

```text
test_tool_call_creates_tool_child
test_async_tool_call_creates_tool_child
test_tool_input_args
test_tool_output_tool_message
test_tool_error_reraised
test_tool_call_id_recorded
test_tool_context_restored
test_no_active_context_tool_hook_is_noop
```

---

# 35. 并发、Retry、Interrupt 测试

```text
test_parallel_tools_are_siblings
test_parallel_async_agent_invocations_are_isolated
test_tool_internal_llm_parent
test_multiple_agents_do_not_share_context
test_model_retry_creates_attempt_spans
test_tool_retry_creates_attempt_spans
test_interrupt_not_marked_as_system_error
```

Retry 需要使用 LangChain 真实 Middleware 或受控 Fake Middleware 冻结顺序语义。

---

# 36. Integration Test：真实 create_agent

禁止只测试自制 Request 对象。

至少使用：

```text
真实 create_agent
Fake Chat Model
真实 LangChain Tool
真实 Middleware Runtime
```

验证：

```text
AGENT
├── LLM
├── TOOL
└── LLM
```

---

# 37. Real E2E：OpenAI-compatible Gateway

运行：

```text
Observed LangChain Agent
→ ChatOpenAI / OpenAI-compatible client
→ LLM Gateway Proxy
→ Fake/OpenAI-compatible upstream
→ Core
→ UI
```

验证数据库中：

```text
AGENT
LLM
TOOL
GATEWAY
```

数量、TraceID 和 ParentSpanID 正确，无重复 LLM。

Tool 内 Model 的 E2E 必须验证：

```text
AGENT
└── TOOL retrieval
    └── LLM
        └── GATEWAY
```

Streaming E2E 必须验证 Trace 生命周期覆盖完整迭代，不要求精确 TTFT。

---

# 38. 性能要求

除网络上报外，单次 Hook 的额外同步 CPU 开销目标：

```text
小型 Metadata < 1 ms
```

Unsampled 不做大 Payload Serialization。

并发场景禁止用全局大锁包裹 Model/Tool Handler。

继续复用现有 Reporter：

```text
Bounded Queue
Batch
Shutdown Drain
Bad Record Isolation
Fail-open
```

不得新建 LangChain 专属 Reporter、Queue 或 Ingest Endpoint。

---

# 39. 推荐实施顺序

## Step 0：Compatibility Spike

验证：

```text
Middleware wrap 顺序
sync/async hook 签名
ToolCallRequest 字段
ModelResponse 字段
Parallel Tool ContextVar
Streaming Agent 生命周期
```

结果必须固化为测试。

## Step 1：Optional Package

实现：

```text
compat.py
optional dependency
清晰 Import Error
```

## Step 2：Agent Wrapper

实现：

```text
invoke / ainvoke / stream / astream
root_mode
metadata mapping
```

## Step 3：Tool Middleware

复用已经冻结的 `Tracer.tool()`，先跑通真实 create_agent Tool Loop。

## Step 4：Logical LLM Span

实现 Provider-neutral LLM Span，并接入 `logical_llm_span_active` 完成 OpenAI 去重。

## Step 5：Async / Parallel / Retry

补全：

```text
awrap_model_call
awrap_tool_call
parallel tools
retry semantics
interrupt semantics
```

## Step 6：Core / UI Metadata

只增加 Framework 展示，不改 SpanKind。

## Step 7：Real E2E

跑通：

```text
AGENT
├── LLM → GATEWAY
├── TOOL
└── LLM → GATEWAY
```

---

# 40. 预计文件变更

```text
sdk/python/pyproject.toml
sdk/python/llm_observability/__init__.py

新增：
sdk/python/llm_observability/integrations/__init__.py
sdk/python/llm_observability/integrations/langchain/__init__.py
sdk/python/llm_observability/integrations/langchain/compat.py
sdk/python/llm_observability/integrations/langchain/agent_wrapper.py
sdk/python/llm_observability/integrations/langchain/middleware.py
sdk/python/llm_observability/integrations/langchain/llm_span.py
sdk/python/llm_observability/integrations/langchain/metadata.py

tests/test_phase2_3_langchain_wrapper.py
tests/test_phase2_3_langchain_middleware.py
tests/test_phase2_3_langchain_dedup.py
tests/test_phase2_3_langchain_concurrency.py
tests/test_phase2_3_langchain_e2e.py
```

原则上不修改：

```text
Proxy Span Ownership
Core Ingest Schema
Database SpanKind
Phase 2.2 Tool Context
```

---

# 41. 回归要求

Direct OpenAI：

```text
AGENT
└── LLM
    └── GATEWAY
```

Manual Tool：

```text
AGENT
└── TOOL
```

LangChain 未安装：

```text
核心 SDK 正常导入和使用
```

LangChain Agent 未包装、Middleware 无 Active Trace：

```text
业务照常执行
不产生孤立 Span
```

---

# 42. 禁止事项

```text
修改 Phase 2.1 Span Ownership 规则
改变 Proxy GATEWAY Parent 逻辑
为 LangChain 新建数据库表
新增 CHAIN SpanKind
自动追踪每个 Runnable
将 LangChain run_id 当作 TraceID
将 thread_id 当作 TraceID
根据 tool_call_id 改 Parent
强制用户启用 OpenAI Instrumentor
吞掉 LangChain 业务异常
```

---

# 43. Definition of Done

```text
LangChain 为可选依赖
```

```text
create_agent invoke/ainvoke 自动创建完整 Agent Trace
```

```text
stream/astream 的 AGENT 生命周期覆盖完整迭代
```

```text
Model Call 自动生成 LLM Span
```

```text
Tool Call 自动生成 TOOL Span
```

```text
普通 LLM 和 Tool 是 AGENT 的兄弟 Span
```

```text
Tool 内 LLM 正确成为 TOOL Child
```

```text
OpenAI 自动插桩与 LangChain LLM 不重复
```

```text
OpenAI-compatible Gateway 仍生成正确 GATEWAY Child
```

```text
Sync / Async / Parallel Context 隔离正确
```

```text
Retry 语义通过真实 Middleware 测试冻结
```

```text
Sampling / Privacy / Size Guard 与 Phase 2.1/2.2 一致
```

```text
无 Active Trace 时 Middleware 不产生孤立 Span
```

```text
Instrumentation 失败不改变 Agent 业务结果
```

```text
真实 create_agent Integration Test 通过
```

```text
真实 Gateway E2E 通过
```

```text
Phase 2.1 / 2.2 全量回归通过
```

全部完成后标记：

```text
Phase 2.3 LangChain Auto Instrumentation
✅ COMPLETE
✅ FROZEN
```

---

# 44. 后续阶段

```text
Phase 2.4 Generic LangChain Runnable / Callback Handler
Phase 2.5 LangGraph Node / Checkpoint / Interrupt
Phase 2.6 MCP Tool Instrumentation
Phase 2.7 CrewAI / AutoGen / LlamaIndex
```

原则：

> 先把 LangChain v1 `create_agent` 做成稳定、可预测、无重复 Span 的 Framework Integration，再扩展到更宽泛的 Runnable 和其他 Agent Framework。
