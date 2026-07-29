# LLM Observability Phase 2.5 最终收口 Bug 修复需求

> 适用仓库：`xjy486/llm-observability`  
> 审查提交：`37ae49f7d7dd82bed7f6ff502dcb478ba013d6b7`  
> 基线提交：`33142e945909b52f1b8307146cc895ca16f8ab89`  
> 阶段：Phase 2.5 — AgentLens SDK Parity Closure  
> 当前结论：主要功能框架已经完成，但 LangChain 自动插桩、完整 Fail-open、Association 全链路一致性、运行时配置生效与真实 E2E 仍存在冻结阻塞。  
> 目标：关闭本轮剩余缺陷，通过全量回归与真实 E2E 后，将 Phase 2.5 标记为 `COMPLETE / FROZEN`。

---

## 1. 修复范围摘要

本轮只处理以下问题：

```text
P0-1 LangChain 自动插桩可靠性与 Invocation 隔离
P0-2 TASK / TOOL 完整 Fail-open
P0-3 Association 全 Span 与 Gateway 一致传播
P0-4 统一 Baggage 编码、解码与优先级
P0-5 max_attribute_bytes / max_payload_bytes / fail_open 真正生效
P0-6 Phase 2.5 真实 E2E 验收
```

同时完成以下 P1 收口：

```text
P1-1 Annotate 使用中立字段并补齐隐私与大小限制
P1-2 Streaming 测试真正验证即时返回和提前关闭
P1-3 set_context / reset_context / Event Sink 故障注入
P1-4 GitHub CI 展示 SDK、Core、Proxy、UI 验证结果
P1-5 冻结 auto_instrument_langchain 默认值与用户文档
```

本轮不进入：

```text
Phase 3 Gateway Native Observability
LangGraph Advanced Workflow
Embedding / Retrieval / Rerank
Qdrant / Bedrock
更多框架自动插桩
```

---

# 2. P0-1：重构 LangChain 自动插桩可靠性

## 2.1 当前问题

当前实现只 Patch：

```python
Runnable.invoke
Runnable.ainvoke
Runnable.stream
Runnable.astream
```

该方案存在以下风险：

1. LangChain 子类可以覆盖上述方法，调用时不一定经过被 Patch 的基类实现。
2. 不能仅凭当前实现保证以下对象全部被自动插桩：

```text
Direct ChatModel
RunnableLambda
RunnableSequence
RunnableParallel
create_agent
CompiledGraph
```

3. `_merge_callback()` 在 `config` 是普通 `dict` 时会原地修改用户传入对象。
4. 用户重复复用同一个 Config 时，后续 Invocation 可能复用第一次注入的 Handler。
5. 并发请求复用同一个 Config 时，可能共享旧 Handler、Registry 和 Span 状态。
6. `CallbackManager` 被包装成：

```python
config["callbacks"] = [existing_manager, handler]
```

该组合必须通过锁定 LangChain 版本的真实运行验证，不能只做静态假设。
7. 当前缺少修复需求中约定的 Direct Model、Sequence、Agent、并发和 CallbackManager 测试。

## 2.2 修复要求

### A. 冻结兼容性入口

先针对锁定版本完成 Compatibility Spike：

```text
langchain==1.3.14
langchain-core==1.5.1
langgraph==1.2.9
langchain-openai==1.4.0
```

验证并选择一个稳定入口：

```text
优先级 1：LangChain 官方 Callback / Config 上下文扩展点
优先级 2：受控包装稳定公开入口
优先级 3：有限、可逆、可测试的 Monkey Patch
```

禁止只依赖 `Runnable` 基类 Patch，除非真实测试证明所有目标对象都会经过该入口。

### B. 用户 Config 必须无副作用

自动插桩不得修改用户原始 Config：

```python
original_config = kwargs.get("config")
config_copy = copy_config(original_config)
config_copy["callbacks"] = merge_callbacks(...)
kwargs = {**kwargs, "config": config_copy}
```

必须满足：

```text
调用前后 original_config 内容相同
调用前后用户 callbacks 对象未被替换
同一个 Config 可安全复用于多次调用
同一个 Config 可安全用于并发调用
```

### C. 每次根 Invocation 独立状态

每次根 Invocation 必须独立创建：

```text
LangChainObservabilityCallbackHandler
CallbackRunRegistry
Chain Event Counters
Span Registry References
Auto Root State
```

不得跨 Invocation 复用。

嵌套 Runnable 调用应识别为同一个根 Invocation，不得重复创建 AGENT Root。

推荐使用 `ContextVar` 保存 Invocation State：

```python
@dataclass
class AutoInvocationState:
    handler: LangChainObservabilityCallbackHandler
    root_context_manager: object
    depth: int
```

根调用：

```text
depth=0 → 创建 Handler 和 AGENT
```

嵌套调用：

```text
depth>0 → 复用本 Invocation Handler，不再创建 AGENT
```

退出根调用时：

```text
close_open_runs()
结束 AGENT
清理 ContextVar
```

### D. 用户 Callback 必须保留

支持：

```text
callbacks=None
callbacks=list[BaseCallbackHandler]
callbacks=CallbackManager
callbacks=AsyncCallbackManager
```

用户 Callback 必须：

```text
仍被调用
调用次数不减少
顺序明确
不被替换
不被永久写入 Config
```

### E. 自动与显式模式去重

以下组合均不得产生重复 Span：

```text
Auto LangChain + observe_runnable
Auto LangChain + observe_agent
Auto LangChain + LangChain Middleware
Auto LangChain + 用户显式 Callback Handler
Auto LangChain + OpenAI Auto
```

硬断言：

```text
每个 Model Attempt：1 个 LLM
每个 Tool Attempt：1 个 TOOL
每个 Provider Attempt：1 个 GATEWAY
每个根 Invocation：最多 1 个 AGENT
```

## 2.3 必须新增的测试

```text
test_langchain_auto_import_before_init
test_langchain_auto_import_after_init
test_langchain_auto_direct_chat_model_invoke
test_langchain_auto_direct_chat_model_ainvoke
test_langchain_auto_runnable_lambda
test_langchain_auto_runnable_sequence
test_langchain_auto_runnable_parallel
test_langchain_auto_create_agent
test_langchain_auto_stream
test_langchain_auto_astream
test_langchain_auto_same_config_reused_sequentially
test_langchain_auto_same_config_reused_concurrently
test_langchain_auto_thread_concurrency
test_langchain_auto_async_concurrency
test_langchain_auto_user_callback_list_preserved
test_langchain_auto_callback_manager_preserved
test_langchain_auto_async_callback_manager_preserved
test_langchain_auto_manual_wrapper_dedup
test_langchain_auto_middleware_dedup
test_langchain_auto_openai_dedup
test_langchain_auto_shutdown_restores_originals
test_langchain_auto_reinit_after_shutdown
```

---

# 3. P0-2：TASK / TOOL 完整 Fail-open

## 3.1 当前问题

TASK 和 TOOL 的 Finalization 当前仍可能传播以下 Telemetry 异常：

```text
span.set_error()
span.set_status()
span.end()
output processing
request_metadata processing
span.to_record()
reporter.report()
reset_context()
```

尤其是 `span.end()`、`set_error()` 和 `to_record()` 缺少内层 `try/except` 时，可能出现：

```text
业务成功 + span.end() 失败
→ 业务调用收到 RuntimeError
```

或者：

```text
业务抛 ValueError + span.end() 失败
→ ValueError 被 Telemetry RuntimeError 覆盖
```

此外，TASK 和 TOOL 在注册 Event Sink 后调用 `set_context()`。如果 `set_context()` 失败，当前路径必须确保 Event Sink 被注销。

## 3.2 修复要求

TASK 和 TOOL 统一使用以下结构：

```python
def __exit__(self, exc_type, exc_val, exc_tb):
    try:
        try:
            # 状态设置
            # 错误记录
            # span.end()
            # output / metadata
            # to_record()
            # reporter.report()
            ...
        except Exception:
            logger.exception("telemetry finalization failed")
    finally:
        safe_unregister_event_sink()
        safe_reset_context()
    return False
```

### A. 业务异常优先

必须保证：

```text
业务成功 + Telemetry 失败
→ 返回原业务结果

业务异常 + Telemetry 失败
→ 原业务异常继续传播
```

### B. `__enter__()` 失败清理

流程应调整为：

```text
创建 Span
处理输入
span.start
注册 Event Sink
set_context
```

任意一步失败时：

```text
已注册 Event Sink → 注销
已创建 Context Token → 恢复
已启动 Span → best-effort end
不影响原业务调用，或按配置错误语义抛出
```

### C. reset_context 本身也必须 Fail-open

```python
try:
    reset_context(token)
except Exception:
    logger.exception("context reset failed")
    best_effort_restore_parent()
```

不得让 Context 清理失败替换业务异常。

## 3.3 故障注入测试

TASK 与 TOOL 分别覆盖：

```text
set_error 失败
set_status 失败
span.end 失败
output processing 失败
request_metadata 失败
to_record 失败
reporter.report 失败
register_event_sink 失败
set_context 失败
unregister_event_sink 失败
reset_context 失败
异常对象 __str__ 失败
```

每种至少验证：

```text
业务成功场景
业务异常场景
Context 恢复
Event Sink 无残留
原异常不被替换
```

建议测试名：

```text
test_task_span_end_failure_preserves_success_result
test_task_span_end_failure_preserves_business_error
test_task_to_record_failure_preserves_success_result
test_task_set_error_failure_preserves_business_error
test_task_set_context_failure_unregisters_event_sink
test_task_reset_context_failure_does_not_replace_business_error

test_tool_span_end_failure_preserves_success_result
test_tool_span_end_failure_preserves_business_error
test_tool_to_record_failure_preserves_success_result
test_tool_set_error_failure_preserves_business_error
test_tool_set_context_failure_unregisters_event_sink
test_tool_reset_context_failure_does_not_replace_business_error
```

---

# 4. P0-3：Association 全 Span 与 Gateway 一致传播

## 4.1 当前问题

当前 Association 基础 Context 已实现，但仍存在以下缺口：

1. `@agent` 显式参数只写入 AGENT Span，没有建立临时 Association Context。
2. 因此 `@agent(user_id=..., session_id=...)` 内部创建的 TASK、TOOL、LLM、GATEWAY 不一定继承。
3. LangChain Callback LLM 创建路径没有统一调用 `apply_association_to_span(span)`。
4. Association 传播逻辑分散在：

```text
decorators.py
association.py
task.py
tool.py
tracer.py
distributed.py
instrumentation/openai.py
callback_spans.py
propagation.py
proxy/trace_context.py
```

容易产生字段、编码和优先级不一致。

## 4.2 修复要求

### A. 建立唯一 Association Resolver

统一入口：

```python
def resolve_association(
    span_explicit=None,
    decorator_explicit=None,
    context=None,
    remote=None,
) -> AssociationProperties:
    ...
```

统一优先级：

```text
Span 显式值
> Decorator 显式值
> Association Context
> Remote Carrier
> None
```

统一字段：

```text
user
session_id
message_id
business_scenario
app_name
```

兼容别名：

```text
user_id → user
business_scene → business_scenario
```

### B. `@agent` 显式属性必须建立临时 Context

进入 `@agent` 时：

```python
assoc_token = set_association_properties({
    "user": user_id,
    "session_id": session_id,
    "message_id": message_id,
    "business_scenario": business_scenario,
})
```

退出时：

```python
reset_association_properties(assoc_token)
```

要求：

```text
AGENT Root 使用显式属性
所有子 Span 继承
嵌套外层 Association 未覆盖字段保留
异常、取消、GeneratorExit、aclose 后恢复
```

### C. 所有 Span 创建路径统一应用

必须覆盖：

```text
Tracer AGENT
Decorator AGENT
TASK
TOOL
Decorator LLM
OpenAI LLM
LangChain Callback LLM
LangChain Callback TOOL
Retriever TOOL
Distributed Client TASK
Distributed Server AGENT
GATEWAY
```

### D. LangChain Callback LLM

`CallbackLLMSpan.__enter__()` 创建 Span 后必须调用：

```python
apply_association_to_span(span)
```

并确保 Callback TOOL、Retriever TOOL 走同一个 Association Resolver。

## 4.3 必须新增的测试

```text
test_agent_explicit_association_inherited_by_task
test_agent_explicit_association_inherited_by_tool
test_agent_explicit_association_inherited_by_llm
test_agent_explicit_association_inherited_by_gateway
test_agent_explicit_association_nested_merge
test_agent_association_restored_after_success
test_agent_association_restored_after_error
test_agent_association_restored_after_generator_close
test_agent_association_restored_after_async_generator_aclose
test_langchain_callback_llm_inherits_association
test_langchain_callback_tool_inherits_association
test_retriever_tool_inherits_association
test_distributed_server_local_override_remote_association
```

---

# 5. P0-4：统一 Baggage 编码、解码与优先级

## 5.1 当前问题

当前存在两套 Baggage 逻辑：

```text
distributed.py：百分号编码与解码
propagation.py：直接拼接原始值
proxy/trace_context.py：直接读取，不解码
```

这会造成以下值解析错误：

```text
逗号
等号
空格
Unicode
控制字符
百分号
```

例如：

```text
user = "alice,bob=1 x"
```

未经编码直接写入 Baggage 时会破坏键值分隔。

## 5.2 修复要求

### A. 提取统一模块

建议新增：

```text
association_propagation.py
```

提供：

```python
encode_baggage_value(value)
decode_baggage_value(value)
build_association_baggage(props)
parse_association_baggage(header)
merge_remote_association(baggage, compat_headers)
```

SDK Distributed、OpenAI Propagation、Proxy 全部复用同一语义。

如果 Proxy 无法直接依赖 SDK 包，则复制为共享契约测试，但字段和算法必须一致。

### B. Compat Header 与 Baggage 优先级

冻结为：

```text
本地 Span 显式值
> 本地 Association Context
> Compat Header
> W3C Baggage
> None
```

或其他明确顺序，但必须：

```text
SDK 和 Proxy 一致
文档明确
测试冻结
```

当前修复需求推荐 Compat Header 覆盖 Baggage，可继续沿用。

### C. 安全约束

Carrier 严禁包含：

```text
Prompt
Response
Tool Input / Output
API Key
Authorization
Cookie
完整请求体
```

Association 值必须：

```text
Fail-closed Sanitization
最大长度限制
控制字符处理
Masking 失败返回 <redacted>
```

## 5.3 必须新增的测试

```text
test_openai_baggage_special_chars_roundtrip
test_distributed_baggage_special_chars_roundtrip
test_proxy_baggage_percent_decode
test_proxy_compat_header_overrides_baggage
test_baggage_unicode_roundtrip
test_baggage_percent_character_roundtrip
test_baggage_control_chars_safely_encoded
test_baggage_masking_failure_is_fail_closed
test_carrier_does_not_include_payload_or_api_key
```

---

# 6. P0-5：运行时配置必须真正生效

## 6.1 当前问题

虽然 `Config` 和 `Observability.init()` 已支持：

```text
max_attribute_bytes
max_payload_bytes
fail_open
```

但运行时仍大量使用模块级常量：

```text
DEFAULT_MAX_PAYLOAD_BYTES
MAX_ATTRIBUTE_SIZE_BYTES
```

并且装饰器公开参数仍默认：

```python
fail_open=True
```

导致全局 `Config.fail_open` 被装饰器默认值覆盖。

## 6.2 修复要求

### A. Payload 限制

所有 Payload 路径必须使用：

```python
tracer.config.max_payload_bytes
```

覆盖：

```text
@agent input/output
@llm input/output
TASK input/output
TOOL input/output
Callback LLM input/output
Retriever content
Annotate input/output
Streaming accumulator
```

调用方式：

```python
apply_size_guard(
    masked,
    max_bytes=tracer.config.max_payload_bytes,
)
```

### B. Attribute 限制

所有 Attribute 路径必须使用：

```python
tracer.config.max_attribute_bytes
```

覆盖：

```text
TaskHandle.set_attribute
ToolHandle.set_attribute
add_event attributes
Annotate attributes
LangChain tags / metadata
Association-derived attributes
```

模块级常量仅作为无 Tracer 时的安全默认值，不得覆盖用户配置。

### C. Streaming Accumulator 预算

创建时显式传入：

```python
BoundedStreamAccumulator(
    max_bytes=tracer.config.max_payload_bytes,
)
```

不能继续使用固定默认预算。

### D. fail_open 默认值

公开装饰器改为：

```python
@agent(..., fail_open: Optional[bool] = None)
@chain(..., fail_open: Optional[bool] = None)
@task(..., fail_open: Optional[bool] = None)
@tool(..., fail_open: Optional[bool] = None)
@llm(..., fail_open: Optional[bool] = None)
```

解析规则：

```text
显式 True / False → 使用显式值
None → 使用 tracer.config.fail_open
SDK 未初始化且无 Config → 默认 True
```

### E. 配置验证

继续保留：

```text
max_attribute_bytes：1 KiB～128 KiB
max_payload_bytes：至少 1 KiB
sample_rate：0.0～1.0
```

建议为 `max_payload_bytes` 增加合理上限，例如：

```text
最大 16 MiB
```

防止误配置导致内存压力。

## 6.3 必须新增的测试

```text
test_agent_respects_custom_max_payload_bytes
test_llm_respects_custom_max_payload_bytes
test_task_respects_custom_max_payload_bytes
test_tool_respects_custom_max_payload_bytes
test_stream_accumulator_uses_custom_max_payload_bytes
test_annotate_respects_custom_max_payload_bytes

test_task_attribute_respects_custom_max_attribute_bytes
test_tool_attribute_respects_custom_max_attribute_bytes
test_annotate_respects_custom_max_attribute_bytes
test_langchain_tags_respect_custom_max_attribute_bytes

test_agent_default_fail_open_uses_global_false
test_chain_default_fail_open_uses_global_false
test_task_default_fail_open_uses_global_false
test_tool_default_fail_open_uses_global_false
test_llm_default_fail_open_uses_global_false
test_decorator_explicit_fail_open_overrides_global
```

---

# 7. P0-6：补齐真实 Phase 2.5 E2E

单元测试不能代替真实调用链验收。本阶段冻结前必须增加以下 E2E。

## 7.1 Scenario A：纯手动 Decorator

```text
@agent
└── @task
    └── @llm
        └── OpenAI Instrumentor
            └── Proxy GATEWAY
```

硬断言：

```text
1 AGENT
1 TASK
1 LLM
1 GATEWAY
GATEWAY.parent_span_id = LLM.span_id
LLM.parent_span_id = TASK.span_id
TASK.parent_span_id = AGENT.span_id
全部 trace_id 相同
无重复 LLM
```

同时验证：

```text
Input / Output
Sampling
Association
Payload Strategy
Size Guard
```

## 7.2 Scenario B：LangChain Auto

只执行：

```python
Observability.init(
    auto_instrument_openai=True,
    auto_instrument_langchain=True,
)
```

不调用：

```text
observe_agent
observe_runnable
langchain_middleware
手动 Callback Handler
```

分别运行：

```text
Direct ChatModel
RunnableSequence
RunnableParallel
create_agent
invoke
ainvoke
stream
astream
```

硬断言：

```text
每个根 Invocation 1 AGENT
每个模型 Attempt 1 LLM
每个工具 Attempt 1 TOOL
每个 Provider Attempt 1 GATEWAY
用户 Callback 仍被调用
```

## 7.3 Scenario C：Association 全链路

设置：

```text
user=alice
session_id=session-1
message_id=message-1
business_scenario=customer-service
```

断言以下记录完全一致：

```text
AGENT
TASK
TOOL
LLM
GATEWAY
```

## 7.4 Scenario D：跨服务追踪

搭建两个本地 HTTP/ASGI 服务：

```text
Service A AGENT
└── TASK client_call
        │ traceparent + baggage
        ▼
Service B AGENT server_call
└── LLM
    └── GATEWAY
```

断言：

```text
全部 trace_id 相同
Server AGENT.parent_span_id = Client TASK.span_id
远端 Sampling 正确继承
Association 正确继承
本地显式值正确覆盖远端值
```

## 7.5 Scenario E：Sampling

`sample_rate=0` 时：

```text
SDK 不上报 AGENT/TASK/TOOL/LLM
仍注入 traceparent，trace_flags=00
Proxy 继承 sampled=0
不执行大 Payload 序列化
```

具体是否让 Proxy GATEWAY 入库，应冻结统一产品语义：

```text
推荐：继承 sampled=0，不入库
```

## 7.6 Scenario F：Streaming

覆盖：

```text
@agent sync generator
@agent async generator
@task sync generator
@task async generator
@tool sync generator
@tool async generator
@llm sync stream
@llm async stream
LangChain stream
LangChain astream
OpenAI stream
```

验证：

```text
首个 Chunk 立即返回
Duration 覆盖完整消费期
提前 close / aclose 正确结束 Span
GeneratorExit / CancelledError 不标记普通 ERROR
Context 恢复
Event Sink 无泄漏
缓存有界
```

---

# 8. P1-1：Annotate 收口

## 8.1 中立字段

禁止无论 SpanKind 都写：

```text
task.input.truncated
task.output.truncated
```

统一改为：

```text
sdk.annotation.input.truncated
sdk.annotation.input.original_size_bytes
sdk.annotation.output.truncated
sdk.annotation.output.original_size_bytes
```

## 8.2 Tags 隐私与大小限制

`tags` 必须执行：

```text
safe_serialize
pattern masking
sensitive key masking
max_attribute_bytes
最大数量限制
单项最大长度限制
```

建议：

```text
最多 32 个 Tag
单项最多 256 字符
```

## 8.3 Span 生命周期保护

`annotate()` 对已结束或已注销的 Span：

```text
返回 False
不修改 Span
不抛异常
```

显式 Span 也必须检查：

```text
span.end_time is None
```

新增测试：

```text
test_annotate_uses_neutral_truncation_keys
test_annotate_tags_are_masked
test_annotate_tags_are_size_guarded
test_annotate_rejects_ended_explicit_span
test_annotate_current_span_after_end_returns_false
```

---

# 9. P1-2：Streaming 测试必须真正验证即时返回

当前只把 Generator 转为 `list()` 再检查结果，不能证明首个 Chunk 立即返回。

正确测试示例：

```python
state = {"tail_executed": False}

@task()
def stream():
    yield "first"
    state["tail_executed"] = True
    yield "second"

gen = stream()
assert next(gen) == "first"
assert state["tail_executed"] is False
```

异步测试：

```python
item = await agen.__anext__()
assert item == "first"
assert state["tail_executed"] is False
```

同时覆盖：

```text
generator.close()
async_generator.aclose()
break 提前退出
CancelledError
无限流消费固定数量后关闭
```

---

# 10. P1-3：资源清理与本地 Registry 泄漏测试

必须验证结束后以下本地状态全部清理：

```text
Span Event Sink Registry
LangChain CallbackRunRegistry
Callback Handler _spans_by_id
Chain Event Counters
Custom Event Counters
Association ContextVar
Span ContextVar
Streaming Wrapper 引用
```

新增压力测试：

```text
连续执行 10,000 次短 Invocation
完成后 Registry 大小回到 0
内存不随完成 Span 数量线性增长
```

建议测试：

```text
test_task_event_sink_registry_empty_after_1000_calls
test_tool_event_sink_registry_empty_after_1000_calls
test_llm_event_sink_registry_empty_after_1000_calls
test_langchain_handler_registry_empty_after_completion
test_langchain_handler_spans_by_id_empty_after_completion
test_stream_close_releases_span_references
```

---

# 11. P1-4：GitHub CI 验收

提交信息中的本地测试结果不足以作为冻结依据。必须让 GitHub Commit 或 PR 显示可见状态检查。

建议工作流：

```text
sdk-tests
core-tests
proxy-tests
ui-typecheck
phase2.5-real-e2e
phase2.1-2.4-regression
```

## 11.1 普通 CI

不使用真实 API Key：

```text
单元测试
Mock E2E
静态检查
UI TypeScript 检查
Proxy 编译 / 导入检查
```

## 11.2 Secret E2E

仅在受信任分支或手动触发运行：

```text
真实 OpenAI-compatible API
真实 Proxy
真实 Core
真实 SDK
```

要求：

```text
API Key 只来自 GitHub Secrets
日志禁止打印 Key
Fork PR 不运行 Secret Job
失败日志进行脱敏
```

冻结前必须看到目标 Commit 的 CI 状态为成功。

---

# 12. P1-5：冻结 LangChain Auto 默认值

当前 `auto_instrument_langchain=False` 与“初始化后自动采集”的产品描述存在歧义。

必须二选一：

## 方案 A：默认开启

```python
auto_instrument_langchain=True
```

适合产品定位：

```text
安装并 init 后即可观测
```

要求 Optional Dependency 不存在时仅 warning，业务正常。

## 方案 B：默认关闭

继续：

```python
auto_instrument_langchain=False
```

但所有文档必须明确写：

```python
Observability.init(
    auto_instrument_langchain=True,
)
```

才能自动观测 LangChain。

本阶段必须冻结一种行为并通过测试，不得继续含糊。

---

# 13. 推荐实施顺序

```text
Step 1  修复 TASK / TOOL 完整 Fail-open
Step 2  让 max_payload_bytes / max_attribute_bytes / fail_open 真正生效
Step 3  @agent 显式 Association 建立临时 Context
Step 4  补齐 Callback LLM / TOOL Association
Step 5  统一 SDK / OpenAI / Proxy Baggage 契约
Step 6  重构 LangChain Auto Invocation 隔离与用户 Config 复制
Step 7  Direct Model / Sequence / Agent / 并发兼容性测试
Step 8  Annotate 与 Streaming 测试收口
Step 9  Real E2E
Step 10 GitHub CI 与 Phase 2.1～2.4 全量回归
```

---

# 14. 重点修改文件

预计修改：

```text
sdk/python/llm_observability/decorators.py
sdk/python/llm_observability/task.py
sdk/python/llm_observability/tool.py
sdk/python/llm_observability/annotation.py
sdk/python/llm_observability/association.py
sdk/python/llm_observability/distributed.py
sdk/python/llm_observability/propagation.py
sdk/python/llm_observability/config.py
sdk/python/llm_observability/instrumentation/langchain.py
sdk/python/llm_observability/instrumentation/openai.py
sdk/python/llm_observability/integrations/langchain/callback_handler.py
sdk/python/llm_observability/integrations/langchain/callback_spans.py
proxy/trace_context.py
proxy/handler.py
```

建议新增：

```text
sdk/python/llm_observability/association_propagation.py

sdk/tests/test_phase2_5_langchain_auto_real.py
sdk/tests/test_phase2_5_fail_open_fault_injection.py
sdk/tests/test_phase2_5_runtime_config.py
sdk/tests/test_phase2_5_association_full_chain.py
sdk/tests/test_phase2_5_streaming_lifecycle.py
sdk/tests/test_phase2_5_registry_cleanup.py
sdk/tests/test_phase2_5_real_e2e.py
```

---

# 15. 验收矩阵

| 能力 | 必须满足 |
|---|---|
| LangChain Direct Model | invoke / ainvoke / stream / astream 自动观测 |
| Runnable | Lambda / Sequence / Parallel 自动观测 |
| Agent | create_agent 自动观测 |
| Invocation 隔离 | 每次根调用独立 Handler 和 Registry |
| 用户 Config | 不被原地修改 |
| 用户 Callback | 不丢失、不替换、调用次数正确 |
| 去重 | 每 Attempt 仅 1 LLM / TOOL / GATEWAY |
| Fail-open | Telemetry 故障不改变业务结果和异常 |
| Association | 全部 SpanKind 与 Gateway 一致 |
| Distributed | Client TASK 与 Server AGENT 同 Trace |
| Baggage | 特殊字符和 Unicode 正确往返 |
| Runtime Config | 大小限制和 fail_open 真正生效 |
| Streaming | 即时返回、有界缓存、close/aclose 正确 |
| Cleanup | Context、Event Sink、Registry 无泄漏 |
| Core/UI | TASK、message_id、chain_count 正确 |
| Regression | Phase 2.1～2.4 全量通过 |
| CI | Commit 上可见且全部成功 |

---

# 16. Definition of Done

只有全部满足以下条件，才能冻结 Phase 2.5：

```text
1. LangChain Auto 对锁定版本的 Direct Model、Runnable、Agent 全部可用
2. 同一 Config 连续与并发复用无 Handler 污染
3. TASK / TOOL 在所有故障注入下完整 Fail-open
4. @agent 显式 Association 继承到 TASK/TOOL/LLM/GATEWAY
5. Callback LLM/TOOL 正确继承 Association
6. OpenAI、Distributed、Proxy 使用一致的 Baggage 契约
7. max_attribute_bytes / max_payload_bytes / fail_open 运行时生效
8. Annotate 使用中立字段并执行完整隐私与大小保护
9. Streaming 首 Chunk 即时返回，close/aclose 后 Context 和 Registry 清理
10. 真实手动 Decorator E2E 通过
11. 真实 LangChain Auto E2E 通过
12. 真实跨服务 E2E 通过
13. Phase 2.1～2.4 全量回归通过
14. GitHub CI 在目标 Commit 上可见且成功
```

完成后标记：

```text
Phase 2.5 — AgentLens SDK Parity Closure
✅ COMPLETE
✅ FROZEN
```

随后进入：

```text
Phase 3 — Gateway Native Observability
```

---

# 17. 本轮禁止事项

修复过程中禁止：

```text
通过降低测试断言规避问题
用测试条件判断跳过未执行路径
继续在多个模块复制 Baggage 编解码逻辑
修改用户传入的 LangChain Config
跨 Invocation 复用 Callback Handler
把 SessionID 用作 TraceID 关联依据
为修复 LangChain Auto Patch 任意 Runnable 实例
新增第二套 Reporter 或第二套 Span Runtime
在 Carrier 中传播 Prompt、Response、API Key 或 Tool Output
未完成 Real E2E 就标记 COMPLETE
```
