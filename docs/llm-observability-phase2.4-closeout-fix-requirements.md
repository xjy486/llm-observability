# LLM Observability Phase 2.4 收口修复需求

> 适用仓库：`xjy486/llm-observability`  
> 审查范围：`e2c7933bcfcb6ca6eca312a4db112b96c1fe79ca` 至 `e389344cc0e2d98c21293e8c37f74ecf1754238b`  
> 当前阶段：Phase 2.4 Generic LangChain Runnable / Callback Instrumentation  
> 当前判断：架构和基础主链路已完成，但仍存在安全、Callback 隔离、Tool 去重、Context 恢复、身份映射和测试假阳性等问题。  
> 本文目标：关闭 Phase 2.4 剩余问题，完成同步、异步、流式和 Phase 2.3 共存 E2E 后，再标记 `COMPLETE / FROZEN`。

---

## 1. 当前实现概况

Phase 2.4 已建立：

```text
ObservedLangChainRunnable
+
LangChainObservabilityCallbackHandler
+
CallbackRunRegistry
+
CallbackLLMSpan
```

已覆盖：

```text
Runnable.invoke / ainvoke / stream / astream
Chain Virtual Run 与 Event
LLM / TOOL / Retriever Callback
OpenAI LLM/GATEWAY 去重
RunnableParallel 基础场景
用户 Callback 基础保留
Real OpenAI-compatible E2E
```

目标结构：

```text
AGENT runnable.rag-chain
├── TOOL retriever
└── LLM
    └── GATEWAY
```

不得出现：

```text
双 LLM
双 TOOL
额外 AGENT
CHAIN/RUNNABLE/PROMPT/PARSER SpanKind
```

---

## 2. 优先级

### P0：冻结阻塞问题

```text
P0-1 仓库历史中提交了真实或疑似真实 API Key
P0-2 CallbackManager 被原地修改，重复调用可能累计 Handler
P0-3 Tool Callback 与 Phase 2.3 Middleware 未去重
P0-4 Callback LLM Context 恢复未使用可靠 finally 语义
```

### P1：关键功能和验收缺口

```text
P1-1 Chat Model Messages 未进入 LLM Input Payload
P1-2 observe_runnable 缺少 session_id/user_id/business_scene
P1-3 复用已有 Trace 时 Chain Event 丢失
P1-4 Streaming 无界缓存全部 Chunk
P1-5 Tool/Retriever/astream 测试存在假阳性或缺失
P1-6 Tool Framework Metadata 与 call_id 不完整
P1-7 Retriever Payload 配置未实现
P1-8 Custom Event 语义不完整
P1-9 Chain Event dropped_count 不准确
P1-10 Callback 内部状态字典并发保护不完整
```


---

## 3. P0-1：API Key 安全事件

### 问题

Real E2E 文件中存在硬编码默认 API Key：

```python
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "<hard-coded-secret>")
```

凭据已经进入 Git Commit。只删除最新文件中的值不够，历史 Commit 仍可读取。

### 立即处理

必须完成：

```text
1. 在 Agnes 平台撤销旧 Key
2. 创建新 Key，并禁止继续使用旧 Key
3. 代码只读取环境变量
4. 清理 Git 历史中的凭据
5. 检查 Fork、CI 日志、缓存和 Artifact
6. 启用 Secret Scanning
```

代码改为：

```python
AGNES_API_KEY = os.getenv("AGNES_API_KEY")

if not AGNES_API_KEY:
    print("AGNES_API_KEY is not set")
    sys.exit(1)
```

禁止输出 Key 前缀、后缀、长度或 Hash。

### Git 历史

使用 `git filter-repo` 或 BFG 清理历史，随后强制更新相关分支和标签。无论是否清理历史，旧 Key 都必须永久撤销。

### Secret Scanning

至少接入一种：

```text
Gitleaks
GitHub Secret Scanning
TruffleHog
```

### 验收

```text
当前代码无硬编码 Key
Git 历史搜索不到旧 Key
旧 Key 已撤销
E2E 缺少环境变量时安全退出
Secret Scan 可以拦截类似凭据
```


---

## 4. P0-2：CallbackManager 不得原地修改

### 问题

当前实现对用户传入的 CallbackManager 调用：

```python
existing.add_handler(handler)
```

每次 Invocation 都会创建新的 Observability Handler，重复调用同一个 Manager 可能形成：

```text
第一次：1 个 Observability Handler
第二次：2 个
第三次：3 个
```

会造成重复 Span、重复 Event、旧 Registry 收到新调用和内存增长。

异常回退也不能用：

```python
config["callbacks"] = [handler]
```

否则会丢弃用户 Callback。

### 修复要求

```text
不修改用户 CallbackManager
不删除用户 Callback
不复用旧 Invocation Handler
每次调用生成独立合并结果
```

对 List：

```python
callbacks = list(existing)
callbacks.append(handler)
```

对 CallbackManager，应复制其 handlers、inheritable_handlers、tags、metadata、parent_run_id 等信息后，构建新的 Manager，再追加本次 Handler。具体构造方式以 `langchain-core==1.5.1` 的真实 API 为准。

若复制失败：

```text
保留用户原 Callback
放弃本次 Observability 注入
记录 warning
业务继续执行
```

### 测试

```text
test_callback_list_not_mutated
test_callback_manager_not_mutated
test_shared_callback_manager_repeated_invokes_no_accumulation
test_callback_merge_failure_preserves_user_callbacks
test_user_callback_called_once_per_invocation
test_observability_span_count_not_grow_across_repeated_invokes
```


---

## 5. P0-3：Tool Middleware 与 Callback 去重

### 问题

LLM Callback 已能在已有 LLM Context 下注册 Virtual Run，但 Tool Callback 仍无条件调用 `tracer.tool()`。

同时启用 Phase 2.3 Middleware 与 Phase 2.4 Callback 时，可能得到：

```text
AGENT
└── TOOL Middleware
    └── TOOL Callback
```

### 修复要求

`on_tool_start()` 创建 Tool Span 前检查：

```python
current = get_current_context()

if current and current.span_kind == SpanKind.TOOL:
    register_virtual_tool_run(
        run_id=run_id,
        parent_run_id=parent_run_id,
        context=current,
    )
    return
```

Virtual Tool State：

```text
virtual=True
context=current
context_owner=False
span=None
token=None
```

`on_tool_end()` 和 `on_tool_error()` 遇到 Virtual Tool 时，仅清理 Registry，不结束 Middleware 创建的已有 TOOL Span。

Retriever 也需要同类去重策略。

### 共存 E2E

使用真实：

```text
create_agent
LangChainObservabilityMiddleware
LangChainObservabilityCallbackHandler
OpenAIInstrumentor
真实 Tool
```

断言：

```text
每个 Model Attempt 仅 1 LLM
每个 Tool Attempt 仅 1 TOOL
LLM count == GATEWAY count
无 TOOL → TOOL 重复嵌套
所有 Span 同一 TraceID
```


---

## 6. P0-4：Callback LLM Context 必须可靠恢复

### 问题

`_finalize_llm_span()` 的状态处理、`span.end()`、Context Reset 和 Reporter 位于同一个 `try` 中。

若以下任一步失败：

```text
str(error)
span.set_error
span.set_status
span.end
```

可能跳过 `reset_context()`。

异步场景中，Token 可能在另一个 `contextvars.Context` 中创建。简单捕获 Reset 错误只能保证业务不失败，不能证明 Context 不泄漏。

### 修复要求

```python
def _finalize_llm_span(state, error=None):
    try:
        try:
            # status / end / report
            ...
        except Exception:
            logger.exception("Callback LLM finalization failed")
    finally:
        _restore_callback_context(state)
```

错误消息使用：

```python
def _safe_callback_error_message(error):
    try:
        return str(error)
    except Exception:
        return "<error message unavailable>"
```

必须通过 Compatibility Spike 确认同步和异步 Start/End Callback 是否运行于同一 `contextvars.Context`。

若天然跨 Context，不能依赖跨 Task Token Reset，应改为 Registry 显式 Parent 解析，并只在真实 Provider/Tool 调用窗口内激活 Context。

### 测试

```text
test_callback_llm_set_error_failure_restores_context
test_callback_llm_span_end_failure_restores_context
test_callback_llm_to_record_failure_restores_context
test_callback_llm_error_str_failure_preserves_business_error
test_callback_llm_sync_start_end_context
test_callback_llm_async_start_end_context
test_callback_llm_no_context_leak_to_next_branch
test_callback_llm_no_context_leak_after_end
```


---

## 7. P1-1：Chat Messages 必须进入 Input Payload

### 问题

`on_chat_model_start()` 接收到 `messages`，但没有把它放入 `invocation_params`；`CallbackLLMSpan` 捕获输入时却读取 `invocation_params["messages"]`。

### 修复

```python
params = dict(invocation_params or {})
params["messages"] = messages
```

输入规范化支持：

```text
SystemMessage
HumanMessage
AIMessage
ToolMessage
多轮消息
Tool Calls
Reasoning/Content Blocks
```

继续执行：

```text
normalize_messages
safe_serialize
mask_payload
apply_size_guard
SerializationBudget
```

### 测试

```text
test_chat_callback_records_human_message
test_chat_callback_records_system_message
test_chat_callback_records_tool_message
test_chat_callback_masks_sensitive_message_content
test_chat_callback_input_size_guard
```


---

## 8. P1-2：observe_runnable 身份字段

API 扩展为：

```python
observe_runnable(
    runnable,
    name="runnable",
    root_mode="auto",
    session_id=None,
    user_id=None,
    business_scene=None,
)
```

`Observability.observe_runnable()` 同步扩展。

支持：

```text
固定值
Callable(input, config)
Callable(config)
Callable()
```

默认映射：

```text
config.configurable.thread_id → session_id
config.metadata.user_id/user → user_id
config.metadata.business_scene → business_scene
```

全部复用 Phase 2.3 的 Fail-closed Identity Sanitization。

测试：

```text
test_runnable_explicit_session_id
test_runnable_thread_id_maps_to_session_id
test_runnable_callable_session_id
test_runnable_user_id_mapping
test_runnable_business_scene_mapping
test_runnable_identity_sensitive_value_masked
test_runnable_identity_masking_failure_returns_redacted
```


---

## 9. P1-3：复用已有 Trace 时 Chain Event 不得丢失

### 问题

Wrapper 自己创建 Root 时可绑定具体 Span 对象；复用已有 Trace 或手动 Callback 模式时只有 `SpanContext`，Handler 找不到可写 Span，Chain Event 会静默丢失。

### 修复建议

建立 SDK 级 Active Span Event Registry：

```text
trace_id + span_id → SpanEventSink
```

Span 创建时注册，结束时移除。Callback 根据 `SpanContext.span_id` 获取 Event Sink。

建议只暴露：

```python
class SpanEventSink:
    def add_event(...)
    def set_attribute(...)
```

不要把整个可变 Span 对象直接塞入 `SpanContext`。

### 测试

```text
test_existing_trace_chain_events_recorded
test_manual_callback_mode_chain_events_recorded
test_nested_existing_tool_chain_events_recorded_on_tool
test_span_event_registry_cleanup
test_event_sink_missing_is_fail_open
```


---

## 10. P1-4：Streaming 必须有界缓存

### 问题

当前 `stream()` 和 `astream()` 将全部 Chunk 放入 List，流结束后才 Size Guard，长输出可能导致内存持续增长。

### 修复

实现有界累积器：

```python
class BoundedStreamAccumulator:
    def append(self, chunk): ...
    def finalize(self): ...
```

追加阶段即控制预算，不得先无限缓存后截断。

可采用：

```text
仅保存最后一个 Chunk
按固定字节数累积
达到预算后停止采集并标记 truncated
```

必须保证：

```text
正常耗尽
close
aclose
GeneratorExit
CancelledError
底层异常
```

均能关闭 Root Trace 和 Handler。

### 测试

```text
test_stream_large_output_memory_bounded
test_astream_large_output_memory_bounded
test_stream_payload_truncated
test_stream_close_cleans_handler
test_astream_aclose_cleans_handler
test_astream_cancel_cleans_handler
```


---

## 11. P1-5：强化 Tool、Retriever 与 astream 验收

### Tool

禁止仅断言结果是 List，必须硬断言：

```text
TOOL count == 1
tool.name 正确
tool.input 正确
tool.output 正确
Parent 正确
Status 正确
```

### Retriever

必须硬断言：

```text
retriever TOOL count == 1
tool.type == retriever
document_count == 2
metadata keys 正确
正文默认未采集
```

### Real Tool E2E

必须真正执行 Tool，覆盖：

```text
Runnable → Tool
Tool → LLM → GATEWAY
```

### astream E2E

真实执行：

```python
async for chunk in observed.astream(...):
    ...
```

断言：

```text
1 AGENT
1 LLM
1 GATEWAY
LLM.parent=AGENT
GATEWAY.parent=LLM
TTFT 存在
AGENT/LLM Duration 覆盖完整流
```


---

## 12. 其他 P1 修复

### Tool Metadata

至少统一：

```text
framework.name=langchain
framework.version
langchain.component=tool
langchain.callback.mode=true
langchain.run_id
langchain.parent_run_id
tool.name
tool.type
tool.call_id
```

`call_id` 只做逻辑关联，不参与 Parent 或 Trace 合并。

### Retriever Payload

新增：

```python
capture_retriever_content=False
```

默认只记录文档数量、Metadata Keys 和内容长度。显式开启正文后仍执行 Masking、Size Guard 和全局预算。

### Custom Event

Event Name 规范化为：

```text
langchain.custom.<normalized-name>
```

必须尊重 SDK 的：

```text
off / metadata_only / masked / full
```

不能固定使用 `"masked"`。

### Chain Event dropped_count

达到上限后应持续更新真实丢弃数量，或在 Handler Close 时写入最终值。

### 并发安全

以下状态也要受锁保护：

```text
_spans_by_id
_chain_event_counts
_custom_event_counts
```

锁只保护字典和计数，不得包裹业务调用、序列化或上报。


---

## 13. 建议文件变更

修改：

```text
sdk/python/llm_observability/integrations/langchain/runnable_wrapper.py
sdk/python/llm_observability/integrations/langchain/callback_handler.py
sdk/python/llm_observability/integrations/langchain/callback_registry.py
sdk/python/llm_observability/integrations/langchain/callback_spans.py
sdk/python/llm_observability/integrations/langchain/runnable_metadata.py
sdk/python/llm_observability/integrations/langchain/compat.py
sdk/python/llm_observability/__init__.py
real_e2e_test_runnable.py
```

可能新增：

```text
sdk/python/llm_observability/span_registry.py
sdk/python/llm_observability/integrations/langchain/stream_accumulator.py
```

建议新增测试文件：

```text
test_phase2_4_callback_manager_isolation.py
test_phase2_4_tool_dedup.py
test_phase2_4_callback_context_safety.py
test_phase2_4_chat_payload.py
test_phase2_4_runnable_identity.py
test_phase2_4_existing_trace_events.py
test_phase2_4_stream_bounded.py
test_phase2_4_real_tool_retriever.py
```


---

## 14. 推荐实施顺序

```text
Step 0  撤销 Key、轮换 Key、清理历史、启用 Secret Scan
Step 1  CallbackManager 隔离
Step 2  Tool Middleware/Callback 去重
Step 3  Callback LLM Context Safety
Step 4  Chat Messages Payload 与 Runnable Identity
Step 5  Existing Trace Event Sink
Step 6  Streaming 有界缓存
Step 7  Tool/Retriever/astream 强验收
Step 8  Tool Metadata、Retriever 配置、Custom Event、Dropped Count、并发锁
Step 9  Phase 2.1/2.2/2.3 全量回归
```

---

## 15. 本轮禁止事项

收口前不要开始：

```text
LangGraph Node Span
Checkpoint Resume
Embedding Span
Vector Store 独立 Span
MCP Instrumentation
CrewAI
AutoGen
LlamaIndex
Batch Trace
```

本轮只处理：

```text
Phase 2.4 Generic Runnable / Callback 最终正确性
```

---

## 16. Definition of Done

### Security

```text
旧 API Key 已撤销
仓库和历史无敏感凭据
Secret Scan 生效
```

### Callback Isolation

```text
用户 CallbackManager 不被修改
重复调用不累计 Handler
合并失败不丢用户 Callback
```

### Dedup

```text
Middleware + Callback
每个 Model Attempt 仅 1 LLM
每个 Tool Attempt 仅 1 TOOL
LLM count == GATEWAY count
```

### Context Safety

```text
Callback LLM 所有异常路径恢复 Context
同步/异步均无泄漏
Telemetry 错误不影响业务
```

### Payload 与 Identity

```text
Chat Messages 正确采集
Tool Input/Output 正确采集
Retriever 正文默认关闭
Streaming 缓存有界
session_id/user_id/business_scene 可用
所有身份字段 Fail-closed
```

### Events

```text
新 Root 和已有 Trace 都能记录 Chain Event
Custom Event 遵循命名和 Payload Strategy
Dropped Count 准确
```

### Tests

```text
Tool 与 Retriever 使用硬断言
RunnableParallel Parent 正确
invoke/ainvoke/stream/astream Real E2E
Middleware + Callback 共存 E2E
Phase 2.1/2.2/2.3 全量回归通过
```

全部满足后，才能标记：

```text
Phase 2.4 Generic LangChain Runnable / Callback Instrumentation
✅ COMPLETE
✅ FROZEN
```

随后进入：

```text
Phase 2.5 LangGraph Node / Checkpoint / Interrupt Resume
```
