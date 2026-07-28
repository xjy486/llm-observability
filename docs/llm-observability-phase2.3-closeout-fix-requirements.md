# LLM Observability Phase 2.3 LangChain 自动插桩收口修复需求

> 适用仓库：`xjy486/llm-observability`  
> 审查范围：`f897d86ee8a150b02c194b43f2b5d07cff2c3f83` 至 `752d6a0d1332f694aed5a0e3e0948b7768b3c997`  
> 当前状态：Phase 2.3 已完成架构实现、同步主链路、真实 `create_agent` 集成测试和同步 Gateway E2E，但仍存在异步链路、Fail-open、Metadata Privacy、Retry / Interrupt、Tool 内嵌 LLM 和 Streaming 提前关闭等收口问题。  
> 本轮目标：完成 Phase 2.3 最终正确性修复与验收，修复后再标记 `Phase 2.3 COMPLETE / FROZEN`。

---

# 1. 当前实现状态

当前已经实现：

```text
ObservedLangChainAgent
├── invoke
├── ainvoke
├── stream
└── astream

LangChainObservabilityMiddleware
├── wrap_model_call
├── awrap_model_call
├── wrap_tool_call
└── awrap_tool_call
```

已经跑通：

```text
AGENT
├── LLM
├── TOOL
└── LLM
```

同步 OpenAI-compatible Gateway 主链路已经具备：

```text
AGENT
└── LLM
    └── GATEWAY
```

并完成：

```text
LangChain v1 create_agent
Provider-neutral Model Middleware
Tool Middleware
OpenAI 同步去重
Parallel Tool
Optional Dependency
版本锁定
Phase 2.1 / Phase 2.2 回归
```

但当前仍不建议直接冻结，问题分为：

```text
P0-1 异步 OpenAI-compatible 链路没有完成
P0-2 Middleware 和 LogicalLLMSpan 没有真正做到 Fail-open
P0-3 LangChain Config Metadata 绕过隐私、序列化和大小保护

P1-1 session_id / user_id Callable 与 thread_id 映射不完整
P1-2 Human-in-the-loop Interrupt 被错误标记为 ERROR
P1-3 Retry 语义没有使用真实 Middleware 验证
P1-4 TOOL → LLM → GATEWAY 没有真实验收
P1-5 Streaming 提前关闭测试存在假阳性
P1-6 AGENT Framework Metadata 不完整
P1-7 Real E2E 日志输出 API Key 片段
```

---

# 2. P0-1：异步 OpenAI-compatible 链路没有完成

## 2.1 当前问题

现有 OpenAI Instrumentor 只覆盖同步调用：

```text
openai.resources.chat.completions.Completions.create
```

没有覆盖：

```text
openai.resources.chat.completions.AsyncCompletions.create
```

Phase 2.3 虽然已经实现：

```text
ainvoke
astream
awrap_model_call
awrap_tool_call
```

但 Fake Model 测试只能证明：

```text
ContextVar 生命周期基本正确
```

不能证明：

```text
异步 ChatOpenAI 请求能注入 traceparent
异步请求能带 ownership marker
Proxy GATEWAY 能挂到 LangChain LLM 下
异步链路不会产生重复 LLM
```

当前 Real E2E 只覆盖：

```text
invoke
stream
```

没有真实覆盖：

```text
ainvoke
astream
```

## 2.2 修复要求

OpenAI Instrumentor 必须同时 patch：

```text
Completions.create
AsyncCompletions.create
```

建议结构：

```python
class OpenAIInstrumentor:
    def __init__(self):
        self._original_sync_create = None
        self._original_async_create = None
```

安装时保存并替换同步、异步方法；卸载时分别恢复。

异步路径必须实现与同步路径一致的语义：

```text
无 Active Context
→ 原样调用

logical_llm_span_active=False
→ 创建 LLM Span
→ 注入 traceparent
→ 调用 AsyncCompletions.create
→ 完成或流式结束后结束 Span

logical_llm_span_active=True
→ 不创建第二个 LLM
→ 仍注入 traceparent
→ 仍注入 X-LLM-OBS-Span-Role: llm
```

## 2.3 异步 Streaming

若 OpenAI Async SDK 返回异步流，必须提供：

```text
AsyncObservedStream
```

至少支持：

```text
__aiter__
__anext__
aclose
__aenter__
__aexit__
```

Span 在以下情况结束：

```text
正常耗尽
aclose()
迭代异常
上下文退出
```

禁止在 `AsyncCompletions.create()` 返回时提前结束 LLM Span。

## 2.4 验收测试

必须新增：

```text
test_openai_async_instrumentor_patches_async_create
test_openai_async_dedup_still_injects_traceparent
test_openai_async_nonstreaming_span_lifecycle
test_openai_async_streaming_span_lifecycle
test_openai_async_uninstrument_restores_original
```

真实 LangChain E2E 增加：

```text
Scenario A:
await observed.ainvoke(...)

Scenario B:
async for chunk in observed.astream(...):
    ...
```

断言：

```text
1 AGENT
N LLM
N GATEWAY
LLM count == GATEWAY count
GATEWAY.parent_span_id == 对应 LLM.span_id
所有 Span 共享一个 TraceID
无重复 LLM
```

---

# 3. P0-2：Middleware 没有真正做到 Fail-open

## 3.1 当前问题

当前 Model Middleware 结构类似：

```python
with LogicalLLMSpan(request):
    return handler(request)
```

如果 `LogicalLLMSpan.__enter__()`、metadata 提取、Span 初始化或 Context 设置发生异常，业务 Handler 不会执行。

Tool Middleware 同样存在：

```text
Instrumentation 初始化失败
→ Tool Handler 未执行
→ 业务 Agent 失败
```

这违反观测 SDK 的核心要求：

> Telemetry 失败不能改变业务执行结果。

## 3.2 Handler 异常与 Instrumentation 异常必须区分

需要区分：

```text
A. Instrumentation 初始化失败
B. Handler 业务异常
C. Instrumentation 结束阶段失败
```

语义：

### A. 初始化失败

```text
记录日志
直接调用原 Handler
业务继续执行
不产生 Span 或产生降级 Span
```

### B. Handler 业务异常

```text
Span 记录 ERROR
异常原样抛出
```

### C. 结束阶段失败

```text
不得覆盖业务返回值
不得覆盖业务异常
Context 必须恢复
```

## 3.3 推荐实现

可抽取统一执行器：

```python
def run_with_observation(scope_factory, handler):
    scope = None
    handle = None

    try:
        scope = scope_factory()
        handle = scope.__enter__()
    except Exception:
        logger.exception("Instrumentation initialization failed")
        return handler()

    business_error = None
    try:
        result = handler()
        try:
            handle.set_response(result)
        except Exception:
            logger.exception("Response capture failed")
        return result
    except BaseException as exc:
        business_error = exc
        raise
    finally:
        try:
            if business_error is None:
                scope.__exit__(None, None, None)
            else:
                scope.__exit__(
                    type(business_error),
                    business_error,
                    business_error.__traceback__,
                )
        except Exception:
            logger.exception("Instrumentation finalization failed")
```

同步、异步版本可分别实现，但必须保持同一语义。

## 3.4 Context 必须放在 finally 恢复

`LogicalLLMSpan.__exit__()` 必须保证：

```python
try:
    # status / error / end / report
finally:
    if self._token is not None:
        reset_context(self._token)
```

即使以下代码失败：

```text
str(exc_val)
set_error
span.end
to_record
reporter.report
```

Parent Context 也必须恢复。

Tool Span 同样检查并保持这一要求。

## 3.5 验收测试

新增：

```text
test_model_instrumentation_enter_failure_still_calls_handler
test_model_instrumentation_exit_failure_preserves_result
test_model_instrumentation_exit_failure_preserves_business_exception
test_model_context_restored_when_error_stringification_fails
test_tool_instrumentation_enter_failure_still_calls_handler
test_tool_instrumentation_exit_failure_preserves_result
test_tool_context_restored_on_instrumentation_failure
```

重点断言：

```text
Handler 调用次数 == 1
返回值完全不变
业务异常类型和对象不变
Parent Context 恢复
Reporter 故障不影响 Agent
```

---

# 4. P0-3：LangChain Config Metadata 绕过隐私与大小保护

## 4.1 当前问题

当前会读取：

```text
config.configurable.thread_id
config.run_name
config.tags
config.metadata
```

但主要只是：

```text
str()
数量截断
长度截断
```

`config.metadata` 可能原样写入 Span Attributes。

风险：

```python
config={
    "metadata": {
        "api_key": "sk-secret",
        "authorization": "Bearer xxx",
        "client": SomeCustomObject(),
    }
}
```

可能造成：

```text
敏感数据直接入库
不可序列化对象导致 AGENT Record 被 Reporter 丢弃
超大 metadata 造成高内存或超大 Span
```

当前定义的：

```text
MAX_METADATA_BYTES = 16 KiB
```

必须真正生效。

## 4.2 修复要求

所有 LangChain Config Metadata 必须统一经过：

```text
safe_serialize
→ mask_payload
→ size guard
```

覆盖：

```text
langchain.metadata
langchain.tags
langchain.thread_id
langchain.run_name
```

推荐新增：

```python
sanitize_langchain_config_metadata(
    config,
    payload_strategy,
) -> dict
```

禁止在 Agent Wrapper 中直接执行：

```python
span.set_attribute(key, raw_value)
```

## 4.3 Metadata 存储位置

结构化小字段可放 Attributes：

```text
langchain.thread_id
langchain.run_name
framework.name
framework.version
langchain.component
```

复杂 Metadata 建议放入：

```text
request_metadata.langchain
```

或严格限制后的：

```text
attributes["langchain.metadata"]
```

无论放哪里，都必须：

```text
JSON-safe
masked
bounded
```

## 4.4 Tags 限制

建议冻结：

```text
最多 50 个 tag
单 tag 最长 128 字符
总序列化大小不超过 8 KiB
```

Tag 文本仍需执行敏感正则：

```text
Bearer ...
sk-...
token
password
secret
cookie
```

## 4.5 验收测试

新增：

```text
test_langchain_metadata_sensitive_keys_are_masked
test_langchain_metadata_sensitive_text_is_masked
test_langchain_metadata_custom_object_is_json_safe
test_langchain_metadata_over_16k_is_truncated
test_langchain_tags_sensitive_text_is_masked
test_langchain_config_does_not_poison_agent_record
```

---

# 5. P1-1：session_id / user_id Callable 和 thread_id 映射不完整

## 5.1 当前问题

当前 Callable 只按：

```python
value()
```

调用。

无法支持规格中的：

```python
session_id=lambda input, config: ...
```

也不能从标准 LangChain 配置中自动映射：

```text
config.configurable.thread_id
→ Trace.session_id
```

## 5.2 修复要求

Wrapper 必须把：

```text
input
config
```

传入 `_AgentScope`。

推荐：

```python
def _resolve_value(value, input, config):
    if not callable(value):
        return value

    for args in (
        (input, config),
        (config,),
        (),
    ):
        try:
            return value(*args)
        except TypeError:
            continue
        except Exception:
            return None

    return None
```

## 5.3 默认映射顺序

`session_id`：

```text
显式固定值
> Callable(input, config)
> config.configurable.thread_id
> None
```

`user_id`：

```text
显式固定值
> Callable(input, config)
> config.metadata.user_id
> config.metadata.user
> None
```

`business_scene`：

```text
显式固定值
> Callable(input, config)
> config.metadata.business_scene
> None
```

不要把 `thread_id` 当作 TraceID，它只映射为：

```text
session_id
langchain.thread_id
```

## 5.4 验收测试

新增：

```text
test_session_id_callable_receives_input_and_config
test_user_id_callable_receives_input_and_config
test_thread_id_maps_to_trace_session_id
test_explicit_session_id_overrides_thread_id
test_callable_failure_is_fail_open
```

---

# 6. P1-2：Human-in-the-loop Interrupt 被错误标记为 ERROR

## 6.1 当前问题

当前任何异常都会进入：

```text
span.set_error(...)
```

因此：

```text
GraphInterrupt
NodeInterrupt
```

会被标记为模型调用错误。

但 Human-in-the-loop 中断是控制流：

```text
暂停等待审批
≠
LLM 或 Tool 故障
```

## 6.2 修复要求

在 Compatibility Layer 中集中识别：

```text
GraphInterrupt
NodeInterrupt
其他已知 LangGraph interrupt 类型
```

例如：

```python
def is_langgraph_interrupt(exc: BaseException) -> bool:
    ...
```

若为 Interrupt：

```text
status = UNSET 或 OK
error_type = null
error_message = null
```

添加 Event 或 Attribute：

```text
langchain.interrupted = true
langchain.interrupt.type = GraphInterrupt
```

然后异常继续抛出。

## 6.3 Trace 状态

若 AGENT 因 Interrupt 退出，AGENT Root 也不能简单标记为系统 ERROR。

推荐统一规则：

```text
Interrupt
→ AGENT status=UNSET
→ langchain.interrupted=true
```

否则平台会把等待人工审批统计进错误率。

## 6.4 验收测试

新增：

```text
test_graph_interrupt_llm_span_not_error
test_graph_interrupt_agent_span_not_error
test_graph_interrupt_is_reraised
test_normal_runtime_error_still_marks_error
```

---

# 7. P1-3：Retry 语义缺少真实验证

## 7.1 当前问题

当前只测试了：

```text
metadata extractor 能读取 node_attempt
```

没有使用真实 Retry Middleware 验证：

```text
Middleware 包装顺序
每次 Attempt 是否产生独立 Span
失败 Attempt 是否保留
成功 Attempt 是否单独记录
```

## 7.2 目标语义

Model Retry：

```text
AGENT
├── LLM attempt=1 ERROR
└── LLM attempt=2 OK
```

Tool Retry：

```text
AGENT
├── TOOL attempt=1 ERROR
└── TOOL attempt=2 OK
```

每次真实 Handler Attempt：

```text
一个独立 SpanID
```

禁止多个 Retry 复用同一个 Span。

## 7.3 Middleware 顺序

必须使用目标锁定版本的真实 LangChain Middleware，验证 Observability Middleware 放置位置，才能得到“一次 Attempt 一个 Span”。

测试完成后，把推荐顺序写入文档。具体顺序以真实测试结果为准，不能凭推测。

## 7.4 验收测试

新增：

```text
test_real_model_retry_creates_span_per_attempt
test_real_tool_retry_creates_span_per_attempt
test_retry_attempt_attribute_matches_runtime
test_retry_final_agent_status_ok
```

---

# 8. P1-4：缺少 TOOL → LLM → GATEWAY 验收

## 8.1 当前问题

当前 calculator Tool 仅返回计算结果：

```python
return x + y
```

它没有在 Tool 内调用模型。

所以当前验证的是：

```text
AGENT
├── LLM
├── TOOL
└── LLM
```

并没有验证：

```text
AGENT
└── TOOL
    └── LLM
        └── GATEWAY
```

## 8.2 修复要求

增加 Tool：

```python
@tool
def retrieval_tool(query: str) -> str:
    return nested_model.invoke(
        [HumanMessage(content=query)]
    ).content
```

必须由真实 LangChain Tool Middleware 包裹。

## 8.3 验收断言

```text
TOOL.parent_span_id == AGENT.span_id
LLM.parent_span_id == TOOL.span_id
GATEWAY.parent_span_id == LLM.span_id
所有 Span 共享同一个 TraceID
```

并确认内部 OpenAI Instrumentor 不重复创建 LLM。

真实 E2E 增加独立 Session ID，避免和普通 Agent Loop 混淆。

---

# 9. P1-5：Streaming 提前关闭测试存在假阳性

## 9.1 当前问题

同步测试：

```python
for item in gen:
    break
```

但仍保留 `gen` 引用。

普通 `break` 不保证 Generator 立即执行：

```text
GeneratorExit
finally
ContextManager.__exit__
```

异步测试同样没有显式：

```python
await agen.aclose()
```

可能只是事件循环结束时触发清理。

## 9.2 修复要求

同步：

```python
gen = observed.stream(...)
next(gen)
gen.close()
```

异步：

```python
agen = observed.astream(...)
await anext(agen)
await agen.aclose()
```

关闭后立即断言：

```text
AGENT 已结束并上报
Context 已恢复
AGENT status 语义正确
```

## 9.3 取消场景

增加：

```text
asyncio.CancelledError
```

场景：

```text
开始 astream
取消 Task
```

断言：

```text
Context 恢复
AGENT 不残留
业务取消异常保持
```

---

# 10. P1-6：AGENT Framework Metadata 不完整

## 10.1 当前问题

LLM 和 TOOL 已有：

```text
framework.name=langchain
framework.version
langchain.component
```

AGENT Root 目前主要写入 Config Metadata，没有统一 Framework 标识。

## 10.2 修复要求

创建 AGENT Root 后固定加入：

```text
framework.name = langchain
framework.version = <locked/runtime version>
langchain.component = agent
langchain.agent.name = <name>
```

可选：

```text
langchain.root_mode
```

这样 Trace Detail 中可以从 Root Span 明确识别：

```text
这是 LangChain Agent Trace
```

## 10.3 验收测试

新增：

```text
test_observed_agent_root_has_framework_metadata
test_real_e2e_agent_has_framework_name_langchain
```

---

# 11. P1-7：Real E2E 不应输出 API Key 片段

## 11.1 当前问题

Real E2E 当前会打印：

```text
API Key 前 10 位 + 后 4 位
```

即使不是完整 Key，也不应出现在：

```text
CI 日志
共享终端日志
构建产物
截图
```

## 11.2 修复要求

改为：

```text
API Key: configured
```

或：

```text
API Key: missing
```

禁止输出前缀、后缀、长度或 Hash。

---

# 12. 测试补充清单

本轮至少新增以下测试。

## Async OpenAI

```text
1. AsyncCompletions.create patch/unpatch
2. async dedup header injection
3. async non-stream LLM lifecycle
4. async stream lifecycle
5. LangChain ainvoke Real Gateway E2E
6. LangChain astream Real Gateway E2E
```

## Fail-open

```text
7. LLM __enter__ 失败仍调用 Handler
8. LLM __exit__ 失败不改变结果
9. LLM Context finally 恢复
10. Tool Instrumentation 初始化失败仍调用 Handler
11. Reporter 失败不改变 Agent
```

## Privacy

```text
12. metadata sensitive key mask
13. metadata sensitive regex mask
14. metadata custom object JSON-safe
15. metadata 16 KiB size guard
16. tag secret mask
```

## Identity Mapping

```text
17. callable(input, config)
18. thread_id → session_id
19. explicit session_id override
```

## Interrupt / Retry

```text
20. GraphInterrupt 非 ERROR
21. Model Retry 每 Attempt 一 Span
22. Tool Retry 每 Attempt 一 Span
```

## Trace Structure

```text
23. TOOL → LLM → GATEWAY
24. stream explicit close
25. astream explicit aclose
26. async cancel context restore
27. AGENT framework metadata
```

---

# 13. 推荐实施顺序

## Step 1：异步 OpenAI Instrumentation

```text
AsyncCompletions patch
AsyncObservedStream
ainvoke/astream Gateway E2E
```

这是 Phase 2.3 冻结前优先级最高的任务。

## Step 2：Fail-open 和 Context Safety

```text
Middleware 初始化失败降级
__exit__ finally reset
业务异常保持
```

## Step 3：Metadata Privacy

```text
Config Metadata 清洗
Tags 清洗
Size Guard
```

## Step 4：Identity Mapping

```text
Callable(input, config)
thread_id → session_id
```

## Step 5：Interrupt / Retry

```text
GraphInterrupt 语义
真实 Retry Middleware 测试
```

## Step 6：剩余 E2E

```text
TOOL → LLM → GATEWAY
stream close
astream aclose
async cancel
```

---

# 14. 本轮禁止事项

在上述问题收口前，不要开始：

```text
Generic LCEL Runnable Instrumentation
任意 LangGraph Node Span
Retriever Span
Embedding Span
CrewAI
AutoGen
LlamaIndex
MCP Auto Instrumentation
```

本轮只做：

```text
Phase 2.3 LangChain create_agent 最终收口
```

---

# 15. Definition of Done

完成后必须满足：

## Async

```text
ainvoke / astream
→ AGENT → LLM → GATEWAY
```

```text
异步 OpenAI 去重正确
```

## Fail-open

```text
Instrumentation 失败
→ Handler 仍执行
→ 返回值和业务异常不变
```

```text
所有 Context 恢复使用 finally 保证
```

## Privacy

```text
Config Metadata / Tags / thread_id / run_name
→ JSON-safe
→ masked
→ bounded
```

## Identity

```text
thread_id 可默认映射为 session_id
```

```text
Callable 可接收 input/config
```

## Interrupt

```text
Human-in-the-loop Interrupt 不计为系统 ERROR
```

## Retry

```text
一次真实 Attempt
→ 一个独立 LLM/TOOL Span
```

## Trace Structure

```text
TOOL → LLM → GATEWAY
真实 E2E 通过
```

## Streaming

```text
close / aclose / cancel
→ AGENT 结束
→ Context 恢复
```

## Metadata

```text
AGENT / LLM / TOOL 均有统一 framework.name=langchain
```

## Regression

```text
Phase 2.1 全量回归通过
Phase 2.2 全量回归通过
Phase 2.3 单测通过
真实同步/异步 E2E 通过
```

满足上述要求后，才标记：

```text
Phase 2.3 LangChain Auto Instrumentation
✅ COMPLETE
✅ FROZEN
```

随后再进入：

```text
Phase 2.4 Generic LangChain Runnable / Callback Instrumentation
```
