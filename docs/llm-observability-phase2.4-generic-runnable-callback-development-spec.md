# LLM Observability Phase 2.4 开发规格：Generic LangChain Runnable / Callback Instrumentation

> 适用仓库：`xjy486/llm-observability`  
> 开发基线：`de0309c615a604f6c1b4ec2ef63f9ad658def577`  
> 前置状态：Phase 2.1、2.2、2.3 已完成并冻结  
> 版本基线：`langchain==1.3.14`、`langchain-core==1.5.1`、`langgraph==1.2.9`、`langchain-openai==1.4.0`

---

## 1. 阶段目标

Phase 2.3 已支持 `create_agent + AgentMiddleware`。Phase 2.4 扩展到通用 LangChain Runnable 和 LCEL：

```python
chain = prompt | model | parser
observed = observe_runnable(chain, name="qa-chain")
result = observed.invoke({"question": "hello"})
```

目标 Trace：

```text
AGENT runnable.qa-chain
└── LLM
    └── GATEWAY
```

Retriever Chain：

```text
AGENT runnable.rag-chain
├── TOOL retriever
└── LLM
    └── GATEWAY
```

本阶段继续使用冻结的：

```text
AGENT / LLM / TOOL / GATEWAY
```

不新增 `CHAIN`、`RUNNABLE`、`PROMPT`、`PARSER` SpanKind。

---

## 2. 核心设计

使用：

```text
ObservedLangChainRunnable
+
LangChainObservabilityCallbackHandler
```

职责：

```text
ObservedLangChainRunnable
├── 创建/复用 AGENT Root
├── 注入 Callback Handler
├── 管理 invoke/ainvoke/stream/astream
└── 捕获 Root Input/Output

Callback Handler
├── chain callbacks      → Virtual Run + AGENT Event
├── model callbacks      → LLM Span
├── tool callbacks       → TOOL Span
├── retriever callbacks  → TOOL Span（tool.type=retriever）
├── retry/custom events  → Event
└── token callback       → TTFT
```

---

## 3. 范围

必须支持：

```text
Runnable.invoke
Runnable.ainvoke
Runnable.stream
Runnable.astream

RunnableSequence
RunnableParallel
RunnableLambda
ChatPromptTemplate
BaseChatModel
BaseLLM
BaseTool
BaseRetriever
Output Parser
自定义 Runnable
```

Callback 必须覆盖：

```text
on_chain_start/end/error
on_chat_model_start
on_llm_start/new_token/end/error
on_tool_start/end/error
on_retriever_start/end/error
on_retry
on_custom_event
```

暂不支持：

```text
batch / abatch
batch_as_completed
LangGraph Node Span
Checkpoint 跨进程续接
Embedding 独立 Span
Vector Store 独立 SpanKind
MCP / CrewAI / AutoGen / LlamaIndex
```

---

## 4. 用户 API

推荐：

```python
from llm_observability.integrations.langchain import observe_runnable

observed = observe_runnable(
    runnable,
    name="qa-chain",
    root_mode="auto",
)

result = observed.invoke(
    {"question": "hello"},
    config={
        "run_name": "qa-request",
        "tags": ["production"],
        "metadata": {"user_id": "u-1"},
    },
)
```

异步与流式：

```python
await observed.ainvoke(...)

for chunk in observed.stream(...):
    ...

async for chunk in observed.astream(...):
    ...
```

手动 Callback 模式只在已有 Trace 中使用：

```python
handler = LangChainObservabilityCallbackHandler()

with Observability.trace("request"):
    chain.invoke(input, config={"callbacks": [handler]})
```

没有 Active Trace 时，Callback Handler 必须 No-op，不创建孤立 Span。

---

## 5. Root Trace 规则

一次：

```text
invoke / ainvoke / stream / astream
```

对应一条 AGENT Trace。

Root：

```text
span_name = runnable.<name>
span_kind = AGENT
```

Attributes：

```text
framework.name = langchain
framework.version
langchain.component = runnable
langchain.runnable.name
```

继续支持：

```text
root_mode=auto
root_mode=create
root_mode=require_existing
```

语义与 `ObservedLangChainAgent` 完全一致。

---

## 6. Wrapper 生命周期

`stream()` 不能在返回 Iterator 时结束 Trace：

```python
def stream(...):
    def iterator():
        with runnable_scope(...):
            yield from runnable.stream(...)
    return iterator()
```

`astream()`：

```python
async def astream(...):
    with runnable_scope(...):
        async for item in runnable.astream(...):
            yield item
```

必须覆盖：

```text
正常耗尽
close()
aclose()
GeneratorExit
CancelledError
底层异常
```

每次调用创建独立 Callback Handler 和独立 Registry，禁止跨 Invocation 复用运行状态。

---

## 7. Callback 注入

必须保留用户已有：

```text
LangSmith Handler
自定义 Handler
Token Counter
业务 Handler
```

规则：

```text
追加 Observability Handler
不替换原 callbacks
不原地修改用户 CallbackManager
不重复注入同一 Handler
```

必须测试：

```text
callbacks=None
callbacks=list
callbacks=CallbackManager
```

---

## 8. Handler 执行模式

Handler 继承：

```python
BaseCallbackHandler
```

并固定：

```python
raise_error = False
run_inline = True
```

所有 Callback 方法使用同步 `def`。

`run_inline=True` 是硬性要求：异步 CallbackManager 若将同步 Handler 放入 Executor，Handler 中设置的 ContextVar 无法影响真实 Model/Tool 调用。同步 Handler 在当前执行上下文内运行，才能让 Callback 创建的 LLM/TOOL Context 覆盖底层调用。

---

## 9. Run Registry

新增：

```python
@dataclass
class CallbackRunState:
    run_id: str
    parent_run_id: str | None
    run_type: str
    name: str
    context: SpanContext | None
    span: Span | None
    token: Token | None
    context_owner: bool
    virtual: bool
    sampled: bool
    first_token_seen: bool
    started_at: float
    ended: bool
```

Registry：

```text
dict[run_id, CallbackRunState]
```

使用 `threading.RLock` 保护增删查，但不得把业务调用包在锁中。

`run_id` 只用于 Callback 生命周期关联，禁止映射为 TraceID 或直接作为 Parent SpanID。

---

## 10. Parent 解析

创建真实 Span 时按顺序寻找 Parent：

```text
1. parent_run_id 对应 Registry Context
2. 当前 ContextVar
3. 都不存在时 No-op
```

`parent_run_id` 需要先映射到 Registry 中保存的 `SpanContext.span_id`，不能直接写入 `parent_span_id`。

---

## 11. Virtual Chain Run

Chain、Prompt、Parser、Runnable 不创建 Span。

它们注册为 Virtual Run：

```text
virtual=True
context=最近真实 Parent Context
```

示例：

```text
AGENT
└── RunnableSequence（virtual）
    └── Prompt（virtual）
        └── LLM
```

真实 Span Tree：

```text
AGENT
└── LLM
```

Virtual Run 仅用于 Parent 传播与 Event。

---

## 12. Chain Event

`on_chain_start/end/error` 在最近真实 Span 上记录：

```text
langchain.chain.start
langchain.chain.end
langchain.chain.error
```

Attributes：

```text
langchain.run_id
langchain.parent_run_id
langchain.run.name
langchain.run.type
langchain.depth
langchain.status
duration_ms
```

默认不在每个 Chain Event 中保存完整 Input/Output。

每个真实 Span 最多 100 个 LangChain lifecycle Event。超限后记录：

```text
langchain.events.truncated=true
langchain.events.dropped_count
```

---

## 13. Root Input / Output

Root AGENT 可捕获：

```json
{
  "input": {},
  "output": {}
}
```

Attributes：

```text
runnable.input.type
runnable.input.size_bytes
runnable.input.truncated
runnable.output.type
runnable.output.size_bytes
runnable.output.truncated
```

统一复用：

```text
safe_serialize
mask_payload
apply_size_guard
SerializationBudget
payload_strategy
```

Unsampled 时不处理大 Payload。

---

## 14. LLM Callback

映射：

```text
on_chat_model_start → LLM Start
on_llm_start        → LLM Start
on_llm_end          → LLM End
on_llm_error        → LLM Error
```

新增 `CallbackLLMSpan`，不要伪造 Phase 2.3 的 `ModelRequest`。

Context：

```python
SpanContext(
    trace_id=parent.trace_id,
    span_id=llm_span_id,
    parent_span_id=parent.span_id,
    span_kind=SpanKind.LLM,
    sampled=parent.sampled,
    logical_llm_span_active=True,
)
```

属性：

```text
framework.name = langchain
framework.version
langchain.component = model
langchain.callback.mode = true
langchain.run_id
langchain.parent_run_id
langchain.run.name
langchain.tags

gen_ai.operation.name
gen_ai.request.model
gen_ai.provider.name
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
gen_ai.usage.total_tokens
```

Chat 输入规范化 `messages`，Completion 输入规范化 `prompts`，禁止直接保存 LangChain 对象。

---

## 15. Streaming 与 TTFT

`on_llm_new_token` 第一次收到非空 Token 或 Content Block 时：

```text
记录 first_token_time
设置 gen_ai.response.ttft_ms
设置 langchain.streaming=true
```

后续 Token 不创建 Event，不逐 Token 上报。

若同时支持协议级 `on_stream_event`，采用：

```text
first signal wins
```

避免重复计算 TTFT。

---

## 16. OpenAI 去重

Callback LLM 激活后：

```text
Callback LLM
→ OpenAI Instrumentor
→ Proxy
```

目标：

```text
AGENT
└── LLM
    └── GATEWAY
```

OpenAI Instrumentor 看到 `logical_llm_span_active=True` 时：

```text
不创建第二个 LLM
继续注入 traceparent
继续注入 ownership marker
```

---

## 17. 与 Phase 2.3 Middleware 去重

同时启用 Middleware 与 Callback 时：

Model Callback：

```text
current.logical_llm_span_active=True
或 current.span_kind=LLM
→ 注册 Virtual LLM Run
→ 不创建新 LLM
```

Tool Callback：

```text
current.span_kind=TOOL
→ 注册 Virtual TOOL Run
→ 不创建新 TOOL
```

必须保证：

```text
每次 Model Attempt 只有 1 个 LLM
每次 Tool Attempt 只有 1 个 TOOL
LLM count == GATEWAY count
```

`create_agent` 默认路径仍是：

```text
observe_agent + LangChainObservabilityMiddleware
```

Phase 2.4 不修改 Phase 2.3 默认行为。

---

## 18. Tool Callback

映射：

```text
on_tool_start → Tracer.tool().__enter__()
on_tool_end   → handle.set_output() + __exit__(None, None, None)
on_tool_error → __exit__(error_type, error, traceback)
```

Input 优先：

```text
inputs
input_str
```

Attributes：

```text
framework.name = langchain
langchain.component = tool
langchain.callback.mode = true
langchain.run_id
langchain.parent_run_id
tool.name
tool.type
tool.call_id
```

结束和 Context 恢复必须使用 `finally`，Telemetry 错误不得影响业务。

---

## 19. Retriever Callback

Retriever 映射为：

```text
SpanKind.TOOL
tool.type = retriever
langchain.component = retriever
```

默认只记录：

```text
retriever.document_count
retriever.document_metadata_keys
retriever.total_content_chars
```

默认：

```python
capture_retriever_content=False
```

文档正文只有显式开启时才采集，并继续执行 Masking、Size Guard 和 Serialization Budget。

Retriever 会进入现有 Tool 指标，UI/API 通过 `tool.type=retriever` 区分。本阶段不新增独立 Retriever 指标。

---

## 20. Retry 与 Custom Event

`on_retry` 不创建 Span，只记录：

```text
langchain.retry
```

包含：

```text
attempt_number
exception_type
next_sleep_ms
run_id
parent_run_id
```

每次真实 Model/Tool Attempt 仍由各自的 Start/End/Error Callback 创建独立 Span。

`on_custom_event`：

```text
Event 名称最大 128 字符
Data 最大 8 KiB
每 Span 最多 50 个
```

Data 必须 JSON-safe、Masked、Bounded。

`on_text` 默认关闭，配置开启后每条最多 2 KiB。

---

## 21. Context 生命周期

真实 LLM/TOOL State：

```text
token != None
context_owner=True
```

Virtual State：

```text
token=None
context_owner=False
```

结束时：

```python
try:
    # status / output / report
finally:
    if state.context_owner:
        reset_context(state.token)
```

End/Error 找不到 Start、重复 End、Callback 顺序异常都必须 No-op + Debug 日志，不得影响业务。

Wrapper 退出时调用：

```python
handler.close_open_runs(reason="wrapper_exit")
```

未完成 Span 标记：

```text
langchain.callback.incomplete=true
```

并 Best-effort 清理。

---

## 22. Sampling、Privacy 与 Fail-open

Sampling：

```text
Root 决策一次
所有 Callback Span 继承 parent.sampled
禁止再次随机采样
```

Privacy 覆盖：

```text
messages/prompts
tool input/output
retriever query/documents
tags/metadata/run_name
custom events
errors
identity fields
```

全部复用现有：

```text
safe_serialize
mask_payload
apply_size_guard
_safe_error_message
_sanitize_identity_value
```

Callback Handler：

```text
raise_error=False
每个 Callback 内部 try/except
Reporter/Span/Metadata 错误均记录但不传播
```

---

## 23. 并发语义

`RunnableParallel` 目标：

```text
AGENT
├── LLM branch-a
├── LLM branch-b
└── TOOL branch-c
```

要求：

```text
SpanID 唯一
TraceID 相同
Parent 正确
ContextVar 不串线
Registry 不丢 Run
```

Tool 内执行 LCEL：

```text
AGENT
└── TOOL
    └── LLM
        └── GATEWAY
```

Direct Model、Direct Tool、Direct Retriever 也必须分别形成：

```text
AGENT → LLM
AGENT → TOOL
AGENT → TOOL(type=retriever)
```

---

## 24. UI 与 Metrics

不新增 Trace Tree 节点类型。

AGENT Detail 增加：

```text
Runnable Name
Chain Event Timeline
Chain Count
Dropped Event Count
```

LLM Detail 增加：

```text
Callback Mode
TTFT
LangChain Run ID
```

Tool Detail显示：

```text
Component: Tool / Retriever
```

继续复用：

```text
trace_count
llm_call_count
tool_call_count
LLM latency
Tool latency
token usage
error rate
```

---

## 25. Compatibility Spike

正式编码前先冻结以下行为：

```text
run_inline=True 在 invoke/ainvoke 中的 ContextVar 行为
on_chain_start/end 顺序
on_chat_model_start 与 Provider 调用顺序
on_tool_start 与 AgentMiddleware 顺序
RunnableParallel 的 run_id/parent_run_id
stream/astream 的 token Callback
Callback List/Manager 合并方式
serialized=None
```

Spike 结果必须转成自动测试。

---

## 26. 测试清单

Wrapper：

```text
invoke / ainvoke
stream / close
astream / aclose
cancel
root_mode
existing callbacks preserved
```

Registry/Chain：

```text
virtual run
nested parent
event limit
end without start
duplicate end
open run cleanup
```

LLM：

```text
chat/completion
usage
error
context restore
TTFT
unsampled
OpenAI dedup
```

Tool/Retriever：

```text
input/output/error
None output
middleware dedup
retriever metadata
retriever content off by default
```

Concurrency：

```text
RunnableParallel siblings
parallel async isolation
Tool internal Runnable
multiple invocation registry isolation
```

Privacy：

```text
metadata/tags masking
custom event bound
retriever masking
root payload guard
identity fail-closed
```

---

## 27. Integration 与 Real E2E

真实 LCEL：

```text
ChatPromptTemplate
→ FakeChatModel
→ StrOutputParser
```

断言：

```text
1 AGENT
1 LLM
0 CHAIN Span
Chain Event 存在
```

RunnableParallel：

```text
1 AGENT
2 LLM
Sibling Parent 正确
```

Retriever Chain：

```text
AGENT
├── TOOL retriever
└── LLM
```

OpenAI-compatible Real E2E：

```text
Observed Runnable
→ ChatOpenAI
→ Proxy
→ Core
```

断言：

```text
1 AGENT
1 LLM
1 GATEWAY
LLM.parent = AGENT
GATEWAY.parent = LLM
LLM count == GATEWAY count
Streaming TTFT 可用
```

Phase 2.3 共存 E2E：

```text
AgentMiddleware
+ Callback Handler
+ OpenAIInstrumentor
```

不得出现双 LLM、双 TOOL 或额外 AGENT。

---

## 28. 推荐实施顺序

```text
Step 0  Compatibility Spike
Step 1  Runnable Wrapper
Step 2  Registry + Virtual Chain Event
Step 3  LLM Callback + TTFT + OpenAI Dedup
Step 4  Tool Callback
Step 5  Retriever Callback
Step 6  Middleware Dedup + RunnableParallel
Step 7  Sync/Async/Streaming Real E2E
Step 8  Phase 2.1/2.2/2.3 Regression
```

---

## 29. 预计文件

新增：

```text
integrations/langchain/callback_handler.py
integrations/langchain/callback_registry.py
integrations/langchain/callback_spans.py
integrations/langchain/runnable_wrapper.py
integrations/langchain/runnable_metadata.py
```

修改：

```text
llm_observability/__init__.py
integrations/langchain/__init__.py
integrations/langchain/compat.py
```

测试：

```text
test_phase2_4_callback_spike.py
test_phase2_4_runnable_wrapper.py
test_phase2_4_chain_events.py
test_phase2_4_llm_callback.py
test_phase2_4_tool_callback.py
test_phase2_4_retriever_callback.py
test_phase2_4_callback_dedup.py
test_phase2_4_callback_concurrency.py
test_phase2_4_callback_privacy.py
test_phase2_4_runnable_e2e.py
```

---

## 30. 禁止事项

```text
新增 CHAIN/RUNNABLE SpanKind
把每个 Runnable 变成 Span
把 run_id 当成 TraceID
Monkey Patch Runnable.invoke
Monkey Patch CallbackManager
替换用户 Callback
逐 Token 上报
默认采集 Retriever 全文
Callback 异常进入业务
修改 Phase 2.3 Middleware 默认语义
实现未冻结的 Batch Trace
```

---

## 31. Definition of Done

完成必须满足：

```text
observe_runnable 支持 invoke/ainvoke/stream/astream
一次调用对应一条 AGENT Trace
Chain/Runnable 使用 Event，不新增 SpanKind
Model 自动创建 LLM
Tool 自动创建 TOOL
Retriever 映射为 TOOL type=retriever
Callback LLM 驱动正确 GATEWAY Parent
Streaming TTFT 正确
RunnableParallel Parent 正确
Middleware + Callback 无重复 Span
无 Active Trace 时 Callback No-op
用户原 Callback 被保留
Callback 全路径 Fail-open
Privacy/Sampling/Size Guard 与前序阶段一致
Real LCEL Sync/Async/Streaming E2E 通过
Phase 2.1/2.2/2.3 全量回归通过
```

全部完成后标记：

```text
Phase 2.4 Generic LangChain Runnable / Callback Instrumentation
✅ COMPLETE
✅ FROZEN
```

后续再进入：

```text
Phase 2.5 LangGraph Node / Checkpoint / Interrupt Resume
```
