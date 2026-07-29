# LLM Observability Phase 2.5 问题修复需求

> 适用仓库：`xjy486/llm-observability`  
> 审查提交：`33142e945909b52f1b8307146cc895ca16f8ab89`  
> 当前阶段：Phase 2.5 — AgentLens SDK Parity Closure  
> 当前结论：TASK 数据模型与公开 API 骨架已经建立，但自动插桩、Sampling、Payload、Association、Annotate、Fail-open 和 Streaming 仍有冻结阻塞问题。  
> 本文目标：关闭 Phase 2.5 的功能正确性和生产可用性问题，通过真实 E2E 与全量回归后再标记 `COMPLETE / FROZEN`。

---

## 1. 当前实现与目标结构

已实现：

```text
TASK SpanKind
@agent / @chain / @task / @tool / @llm
Observability.annotate()
Association Properties + message_id
Distributed Client / Server Helpers
Instruments / block_instruments
LangChain Auto Instrumentor
Core / Storage / Query / UI TASK 支持
```

目标 Trace：

```text
AGENT
├── TASK
├── TOOL
└── LLM
    └── GATEWAY
```

分布式目标：

```text
Service A
AGENT
└── TASK client_call
        │ traceparent + baggage
        ▼
Service B
AGENT server_call
└── LLM
    └── GATEWAY
```

---

## 2. 优先级

### P0：冻结阻塞

```text
P0-1 重构 LangChain 自动插桩
P0-2 修复 @agent / Server Root Sampling
P0-3 补齐 @agent / @llm Input 与 Output
P0-4 修复 @llm 错误 Finalization
P0-5 Association 传播到全部 Span 与 Gateway
P0-6 Annotate 打通 TASK / TOOL / LLM
P0-7 TASK / TOOL 完整 Fail-open
P0-8 Generator 改为真正 Streaming 且缓存有界
```

### P1：关键收口

```text
P1-1 max_attribute_bytes / max_payload_bytes / fail_open 真正生效
P1-2 修复 chain_count
P1-3 修复 Association shutdown 清理
P1-4 Association 嵌套上下文改为合并
P1-5 完善 Distributed Carrier
P1-6 冻结 LangChain Auto 默认值
P1-7 补齐真实 Phase 2.5 E2E
```


---

## 3. P0-1：重构 LangChain 自动插桩

### 当前问题

当前只 Patch：

```python
langchain_core.runnables.config.ensure_config
```

但 LangChain 模块常在导入时执行：

```python
from langchain_core.runnables.config import ensure_config
```

因此用户先导入 LangChain、后执行 `Observability.init()` 时，旧引用可能继续被调用。

另外当前所有调用复用一个全局 Handler 和一组：

```text
_auto_trace
_root_run_id
CallbackRunRegistry
```

并发线程或并发 asyncio Task 可能相互污染。Root 只在 `on_chain_start` 创建，直接 `ChatModel.invoke()` 也可能没有 AGENT Root。

### 修复要求

每个根 Invocation 必须独立创建：

```text
LangChainObservabilityCallbackHandler
CallbackRunRegistry
Auto Root State
```

禁止跨请求复用同一个 Run State。

必须支持：

```text
LangChain import 在 init 之前或之后
ChatModel.invoke
RunnableSequence / RunnableParallel
create_agent
invoke / ainvoke / stream / astream
线程并发
asyncio 并发
用户 CallbackManager
显式 observe_runnable / Middleware 共存
```

若使用 Patch，必须：

```text
可逆、幂等、线程安全
不修改用户 CallbackManager
不替换用户 Callback
不 Patch 任意 Runnable 实例
shutdown 后完整恢复
```

去重硬断言：

```text
每个 Model Attempt 仅 1 LLM
每个 Tool Attempt 仅 1 TOOL
每个 Provider Attempt 仅 1 GATEWAY
```

新增测试：

```text
test_langchain_auto_import_before_init
test_langchain_auto_import_after_init
test_langchain_auto_direct_chat_model
test_langchain_auto_runnable_sequence
test_langchain_auto_create_agent
test_langchain_auto_thread_concurrency
test_langchain_auto_async_concurrency
test_langchain_auto_user_callbacks_preserved
test_langchain_auto_manual_wrapper_dedup
test_langchain_auto_shutdown_restores_originals
```


---

## 4. P0-2：修复 Root Sampling

### 当前问题

`@agent` 先创建 `sampled=True` 的 Context，之后虽计算随机结果，却没有写回 Context或用于最终上报。`sample_rate=0` 时仍可能完整采集。Server Helper 无远端 Carrier 时也固定 sampled。

### 修复要求

必须先采样，再创建 Context：

```python
sampled = random.random() < tracer.config.sample_rate

ctx = SpanContext(
    trace_id=trace_id,
    span_id=span_id,
    parent_span_id=None,
    span_kind=SpanKind.AGENT,
    sampled=sampled,
)
```

Root Manager 保存本地 `_sampled`，Reporter 依据该值决策。

Server Helper：

```text
合法远端 trace_flags → 继承 sampled
无合法 Carrier → 使用本地 sample_rate
```

Unsampled：

```text
保留并传播 Trace Context
不执行大 Payload 序列化
不上报 Record
子 Span 继承 sampled=False
```

测试：

```text
test_agent_sample_rate_zero
test_agent_sample_rate_one
test_agent_children_inherit_sampling
test_server_new_trace_respects_sample_rate
test_server_remote_sampling_inherited
test_unsampled_traceparent_propagated
test_unsampled_payload_not_serialized
```


---

## 5. P0-3：补齐 @agent / @llm Input 与 Output

### 当前问题

当前两类装饰器只创建和结束 Span，没有绑定函数参数，也没有保存返回值。

必须生成：

```text
AGENT payload.input / payload.output
LLM payload.input / payload.output
```

### 修复要求

统一复用：

```text
_bind_arguments
safe_serialize
mask_payload
apply_size_guard
SerializationBudget
BoundedStreamAccumulator
```

输入要求：

```text
支持 args/kwargs 和默认参数
跳过 self/cls
绑定失败安全降级
```

输出要求：

```text
普通返回值
None
同步 Generator 有界累积
异步 Generator 有界累积
```

遵守：

```text
off / metadata_only / masked / full
```

`@llm` 尝试提取：

```text
model / provider / messages / prompt
input_tokens / output_tokens / total_tokens
finish_reason
```

提取失败时省略，不影响业务。

测试：

```text
test_agent_input_output
test_agent_none_output
test_agent_payload_strategy_off
test_agent_payload_size_guard
test_llm_input_output
test_llm_messages_capture
test_llm_model_extraction
test_llm_payload_size_guard
```


---

## 6. P0-4：修复 @llm 错误 Finalization

当前导入别名为 `_safe_error_message`，但 Finalizer 调用了未定义的 `_safe_tool_error_message`。

改为：

```python
error_message=_safe_error_message(exc_val)
```

错误路径必须保证：

```text
设置 ERROR
结束 Span
上报 Record
注销 Event Sink
恢复 Context
重新抛出原始业务异常
```

Telemetry 异常不得替换业务异常。

测试：

```text
test_llm_business_error_reported
test_llm_error_span_ended
test_llm_error_context_restored
test_llm_error_string_failure
test_llm_reporter_failure_preserves_business_error
```


---

## 7. P0-5：统一 Association 传播

### 修复范围

所有 Span 创建路径统一调用：

```python
apply_association_to_span(span)
```

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
Distributed Client TASK
Distributed Server AGENT
```

`@agent` 显式：

```text
user_id / session_id / message_id / business_scenario
```

必须在 Agent 生命周期内建立临时 Association Context，使所有子 Span 继承；退出时恢复。

### Gateway 传播

Header 或 Baggage 传播：

```text
user
session_id
message_id
business_scenario
app_name
```

禁止传播 Prompt、Response、Tool Output、API Key。

统一优先级：

```text
Span 显式值
> Decorator 显式值
> Association Context
> Remote Carrier
> None
```

真实 E2E 断言 AGENT/TASK/TOOL/LLM/GATEWAY 的四个业务关联字段完全一致。


---

## 8. P0-6：打通 Annotate 与子 Span

`annotate(span=None)` 依赖当前 Context 对应的 SpanEventSink。TASK 和 TOOL 等 Span 必须在：

```text
进入时注册 Event Sink
退出 finally 注销
set_context 失败时注销
span.end / reporter 失败时仍注销
```

覆盖：

```text
TASK
TOOL
Decorator LLM
OpenAI LLM
LangChain Callback LLM/TOOL
Distributed Client/Server
```

Annotation 不应无条件写 `task.input.*`。建议统一采用：

```text
sdk.annotation.input.truncated
sdk.annotation.output.truncated
sdk.annotation.*.original_size_bytes
```

测试：

```text
test_annotate_inside_agent
test_annotate_inside_task
test_annotate_inside_tool
test_annotate_inside_llm
test_annotate_explicit_span
test_annotate_event_sink_cleanup
test_annotate_after_span_end_returns_false
```


---

## 9. P0-7：TASK / TOOL 完整 Fail-open

### 要求

TASK 和 TOOL Finalization 使用：

```python
try:
    try:
        # status / error / end / payload / to_record / report
        ...
    except Exception:
        logger.exception("telemetry finalization failed")
finally:
    unregister_event_sink()
    safe_restore_context()
```

必须保证：

```text
业务成功 + Telemetry 失败 → 业务结果不变
业务失败 + Telemetry 失败 → 原始业务异常不变
```

故障注入：

```text
set_error / set_status 失败
span.end 失败
output processing 失败
to_record 失败
reporter.report 失败
reset_context 失败
```

TASK 与 TOOL 均需覆盖。


---

## 10. P0-8：Generator 改为真正 Streaming

禁止：

```python
result = list(generator)
yield from result
```

必须边返回、边有界采集：

```python
for item in generator:
    accumulator.append(item)
    yield item
```

异步同理。复用 Phase 2.4 的 `BoundedStreamAccumulator`。

达到预算后：

```text
停止采集 Telemetry Output
继续返回全部业务 Chunk
记录 truncated=true
```

生命周期覆盖：

```text
正常耗尽
close / aclose
GeneratorExit
CancelledError
消费者提前停止
底层异常
无限流
```

测试：

```text
test_task_sync_generator_first_item_immediate
test_tool_sync_generator_first_item_immediate
test_task_async_generator_bounded
test_tool_async_generator_bounded
test_generator_early_close
test_async_generator_aclose
test_infinite_stream_no_unbounded_memory
test_stream_duration_covers_consumption
```


---

## 11. P1 收口项

### 11.1 配置真正生效

运行时必须使用：

```text
tracer.config.max_attribute_bytes
tracer.config.max_payload_bytes
tracer.config.fail_open
```

`max_attribute_bytes` 校验范围：

```text
1 KiB ～ 128 KiB
```

Decorator 的 `fail_open` 建议改为 `None`：

```text
显式值不为 None → 使用显式值
否则 → 使用全局 Config
```

### 11.2 修复 chain_count

不要使用脆弱的字符串 LIKE。优先：

```sql
json_extract(s.attributes, '$."task.type"') = 'chain'
```

若不能依赖 SQLite JSON1，则增加独立 `task_type` 列。

### 11.3 Association 清理

不要访问不存在的 `_ASSOCIATION_VAR.default`。增加：

```python
clear_association_properties()
```

内部设置为空 `AssociationProperties()`。

### 11.4 Association 嵌套合并

内层未指定字段继承外层，显式字段覆盖：

```text
外层 user=alice, session=s1
内层 message_id=m2
→ 内层仍保留 user 和 session
```

### 11.5 Distributed Carrier

补充：

```text
app_name
W3C Baggage 合法编码
逗号/等号/空格/Unicode/控制字符处理
```

冻结 `inject_carrier()` 语义。建议原地修改并返回同一个对象：

```python
returned is headers
```

明确标准 baggage 与兼容 Header 的优先级。

### 11.6 LangChain Auto 默认值

若产品宣称 init 后自动采集，则默认设为 `True`；若保留 `False`，文档必须明确需要显式开启。


---

## 12. Real E2E

### Scenario 1：纯手动 Decorator

```text
@agent
└── @task
    └── @llm
        └── GATEWAY
```

硬断言：

```text
1 AGENT / 1 TASK / 1 LLM / 1 GATEWAY
Parent 正确
Input/Output 正确
```

### Scenario 2：LangChain Auto

只调用：

```python
Observability.init(auto_instrument_langchain=True)
```

覆盖：

```text
Direct ChatModel
RunnableSequence
create_agent
invoke / ainvoke / stream / astream
```

### Scenario 3：Association

AGENT/TASK/TOOL/LLM/GATEWAY 的 user、session_id、message_id、business_scene 一致。

### Scenario 4：跨服务

使用两个本地 HTTP/ASGI 服务验证：

```text
Client TASK → traceparent/baggage → Server AGENT → LLM → GATEWAY
```

### Scenario 5：Sampling

`sample_rate=0` 时不入库，但仍传播 sampled=0 的 traceparent。

### Scenario 6：Streaming

覆盖同步/异步 Generator、LangChain stream、OpenAI stream，以及提前 close/aclose。


---

## 13. 推荐实施顺序

```text
Step 0  修复 @llm NameError
Step 1  修复 Root Sampling
Step 2  完成 @agent/@llm Input/Output
Step 3  TASK/TOOL Event Sink 与完整 Fail-open
Step 4  Generator 真 Streaming + 有界缓存
Step 5  Association 全 Span/Gateway 传播
Step 6  重构 LangChain Auto，保证 Invocation 隔离
Step 7  配置、chain_count、Association 嵌套与清理
Step 8  Distributed Carrier 完整化
Step 9  Real E2E
Step 10 Phase 2.1～2.4 全量回归
```

重点修改：

```text
decorators.py
task.py
tool.py
annotation.py
association.py
distributed.py
instrumentation/langchain.py
instrumentation/openai.py
config.py
tracer.py
__init__.py
core/storage/db.py
```

建议新增：

```text
test_phase2_5_langchain_auto.py
test_phase2_5_sampling.py
test_phase2_5_payload.py
test_phase2_5_association_e2e.py
test_phase2_5_annotate_children.py
test_phase2_5_fail_open_fault_injection.py
test_phase2_5_streaming.py
test_phase2_5_distributed_e2e.py
test_phase2_5_core_summary.py
```


---

## 14. Definition of Done

### Decorator

```text
@agent/@chain/@task/@tool/@llm
sync/async/generator/async generator 正确
Input/Output 完整
Sampling 正确
Fail-open 正确
```

### LangChain Auto

```text
不同 Import 顺序均生效
Direct Model/Runnable/Agent 均生效
并发请求隔离
用户 Callback 保留
自动与手动无重复 Span
shutdown 完整恢复
```

### Association 与 Annotate

```text
AGENT/TASK/TOOL/LLM/GATEWAY 全部继承关联字段
@agent 显式属性对子 Span 生效
嵌套上下文合并
异常/取消后无泄漏
Annotate 在 AGENT/TASK/TOOL/LLM 内均可用
Event Sink 无泄漏
```

### Streaming

```text
首个 Chunk 立即返回
缓存有界
close/aclose 正确
无限流不 OOM
Duration 覆盖消费期
```

### Distributed

```text
Client TASK 与 Server AGENT 同 Trace
Server Parent 正确
Sampling 与 Association 正确传播
Carrier 符合标准且无敏感 Payload
```

### Core/UI/Tests

```text
TASK/message_id 正确入库和查询
task_count/chain_count 正确
UI TASK/CHAIN/CLIENT CALL 正确展示
Phase 2.1～2.4 全量测试通过
真实 Phase 2.5 E2E 通过
GitHub CI 可见且成功
```

全部满足后才能标记：

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

## 15. 本轮禁止事项

修复完成前不要进入：

```text
Phase 3 Gateway Native
LangGraph
Embedding / Retrieval / Rerank
Qdrant
Bedrock
更多框架
```

本轮只处理 Phase 2.5 的正确性与收口。
