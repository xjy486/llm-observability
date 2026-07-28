# LLM Observability Phase 2.2 开发规格：Tool Span

> 适用仓库：`xjy486/llm-observability`  
> 基线提交：`fd39da9b7b89ba2d4889a76e965a759fd5a42c06`  
> 前置状态：Phase 2.1 Application SDK & Agent Trace 已完成并冻结  
> 文档类型：开发规格 / Technical Development Spec  
> 本阶段目标：在现有 `AGENT → LLM → GATEWAY` Trace 基础上，引入可稳定使用的 `TOOL` Span，使一次 Agent 业务任务中的工具调用能够被完整追踪、展示和统计。

---

# 1. 背景

Phase 2.1 已完成：

```text
Trace
└── AGENT
    └── LLM
        └── GATEWAY
```

当前系统已经能够表达：

```text
一次业务任务
→ 一次或多次 LLM 调用
→ Proxy / Gateway 网络边界
```

但真实 Agent 通常不是只有 LLM：

```text
用户任务
  ↓
LLM 推理
  ↓
调用搜索
  ↓
LLM 推理
  ↓
调用终端
  ↓
LLM 总结
```

因此下一阶段必须支持：

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

Phase 2.2 的重点不是适配某一个 Agent Framework，而是先冻结：

> **Tool Span 的生命周期、父子关系、Payload/Privacy、错误语义、Metrics 和 UI Contract。**

---

# 2. 当前代码基础

当前代码已经预留：

```text
SpanKind.TOOL
```

并且 Public SDK 文档中已经预留：

```text
Observability.tool(...)
```

但尚未实现完整 Tool Span 生命周期。

Core 当前的 Canonical Span Schema 已经是通用 Span 模型，可以容纳：

```text
AGENT
LLM
TOOL
GATEWAY
INTERNAL
```

因此本阶段原则上：

```text
不新增新的 Trace 模型
不新增另一套 Tool 数据库
不改变 TraceID / SpanID 语义
```

Tool 只是现有 Trace 中的一种 Child Span。

---

# 3. 本阶段目标

Phase 2.2 必须完成：

1. `Observability.tool()` 手动 Tool Span API；
2. TOOL Span 正确继承当前 Trace Context；
3. TOOL Span 支持同步和异步上下文；
4. TOOL Span 支持 input/output capture；
5. Tool Payload 复用现有：
   - `off`
   - `metadata_only`
   - `masked`
   - `full`
6. Tool 异常自动记录 ERROR，同时业务异常继续抛出；
7. 支持 Tool 内部调用 LLM；
8. 支持嵌套 Tool；
9. 支持 Tool Decorator；
10. Core / UI 正确展示 TOOL；
11. 增加基础 Tool Metrics；
12. 不破坏 Phase 2.1：
    - OpenAI Instrumentation
    - Sampling
    - Reporter
    - Proxy Ownership
    - No-SDK fallback。

---

# 4. 非目标

本阶段明确不做：

```text
LangChain 自动 Tool Instrumentation
CrewAI / AutoGen / LlamaIndex 自动适配
MCP 自动拦截
OpenAI tool_calls 自动执行
自动把 LLM tool_call 与 Python function 绑定
Span Links
跨进程 Tool Context 传播
线程池自动 Context 传播
Tool Retry 自动拆分
Tool Cost 估算
Tool 权限治理
Tool 安全审计系统
OTLP Collector
Gateway Native Instrumentation
```

本阶段先解决：

> **业务代码显式使用 SDK 时，Tool Span 能稳定、正确地成为 Agent Trace 的一部分。**

---

# 5. 最终 Trace 语义

标准 Agent 流程：

```text
Trace ABC

└── AGENT
    ├── LLM #1
    │   └── GATEWAY #1
    ├── TOOL web_search
    ├── LLM #2
    │   └── GATEWAY #2
    └── TOOL terminal
```

---

# 6. 最重要的父子关系规则

Tool Span 的 Parent **只由调用发生时的当前 Context 决定**。

不要根据：

```text
“这个 Tool 是由哪一个 LLM 生成的 tool_call”
```

强行修改 Span Parent。

---

## 6.1 普通 LLM → Tool 流程

典型代码：

```python
with Observability.trace("research-task"):
    response = client.chat.completions.create(...)

    with Observability.tool(name="web_search"):
        search(...)
```

OpenAI 调用返回后，LLM Context 已经结束并恢复 AGENT。

因此正确结构：

```text
AGENT
├── LLM
│   └── GATEWAY
└── TOOL
```

不是：

```text
AGENT
└── LLM
    ├── GATEWAY
    └── TOOL   ❌
```

原因：

```text
LLM 是一次已完成的模型调用
Tool 是后续执行步骤
```

二者在普通 Agent Loop 中是兄弟 Span。

---

## 6.2 Tool 内部调用 LLM

代码：

```python
with Observability.tool(name="retrieval_tool"):
    client.chat.completions.create(...)
```

当前 Context 是 TOOL。

正确：

```text
AGENT
└── TOOL retrieval_tool
    └── LLM
        └── GATEWAY
```

OpenAI Instrumentor 必须自然读取当前 TOOL Context，并创建 Child LLM。

禁止特殊判断：

```text
只有 AGENT 才能成为 LLM Parent
```

---

## 6.3 Nested Tool

```python
with Observability.tool(name="research"):
    with Observability.tool(name="web_search"):
        ...
```

正确：

```text
AGENT
└── TOOL research
    └── TOOL web_search
```

Nested Tool 是合法行为。

---

## 6.4 Tool 在 Trace 外调用

```python
Observability.init(...)

with Observability.tool(name="search"):
    ...
```

当前没有 Active Trace。

Phase 2.2 冻结规则：

```text
直接抛 RuntimeError
```

推荐错误：

```text
Observability.tool() requires an active trace.
Create a business trace with Observability.trace() first.
```

不要自动创建：

```text
TOOL Root Trace
```

也不要偷偷创建：

```text
AGENT Root Trace
```

原因：

```text
Trace = 一次业务任务
```

业务边界必须由 Application 明确创建。

---

# 7. Public API

新增：

```python
with Observability.tool(
    name="web_search",
    tool_type="search",
    input={"query": "LLM observability"},
    call_id="call_abc123",
) as tool:
    result = web_search(...)

    tool.set_output(result)
```

最终：

```text
span_kind = TOOL
span_name = tool.web_search
```

---

# 8. `Observability.tool()` API 定义

建议：

```python
@classmethod
def tool(
    cls,
    name: str,
    tool_type: str | None = None,
    input: Any = None,
    call_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> ToolContextManager:
    ...
```

参数：

### `name`

必填。

例如：

```text
web_search
terminal
file_read
database_query
calculator
mcp.github.search
```

要求：

```text
不能为空
不得包含动态用户输入
不得把 Token / URL 参数 / SQL 正文拼进 name
```

推荐最大长度：

```text
128 chars
```

---

### `tool_type`

可选，自由字符串。

推荐常见值：

```text
function
search
retrieval
http
database
shell
filesystem
calculator
mcp
custom
```

不要在 Phase 2.2 做强 Enum 校验。

原因：

```text
Tool 生态开放
过早枚举会限制扩展
```

---

### `input`

可选。

支持任意 Python 对象：

```text
dict
list
str
int
float
bool
dataclass
Pydantic model
其他可安全 repr 对象
```

最终必须经过：

```text
Safe Serialization
→ Privacy Strategy
→ Size Guard
```

后再进入 telemetry。

---

### `call_id`

可选。

用于保存来自模型的 Tool Call ID，例如：

```text
call_abc123
```

写入：

```text
tool.call_id
```

注意：

```text
call_id 只用于逻辑关联
不改变 parent_span_id
```

Phase 2.2 不实现 Span Links。

---

### `attributes`

可选扩展属性。

禁止用户通过 attributes 存放大 Payload。

---

# 9. Tool Span 属性规范

TOOL Span 建议属性：

```text
tool.name
tool.type
tool.call_id
tool.input.type
tool.output.type
tool.input.size_bytes
tool.output.size_bytes
tool.input.truncated
tool.output.truncated
```

示例：

```json
{
  "tool.name": "web_search",
  "tool.type": "search",
  "tool.call_id": "call_abc123",
  "tool.input.type": "dict",
  "tool.output.type": "list",
  "tool.input.size_bytes": 112,
  "tool.output.size_bytes": 8192,
  "tool.output.truncated": false
}
```

不要放：

```text
tool.input = 完整 JSON
tool.output = 完整文本
```

到 `attributes`。

大内容必须走：

```text
payload
```

---

# 10. Tool Payload Contract

复用现有 Canonical Record：

```json
{
  "payload": {
    "input": "...",
    "output": "..."
  },
  "request_metadata": {
    "tool_name": "web_search",
    "tool_type": "search",
    "call_id": "call_abc123"
  }
}
```

Phase 2.2 不实现独立 Payload Store。

继续使用当前 Core 已有的：

```text
payload
request_metadata
```

字段。

不要在本阶段重新开启：

```text
payload_ref external store
```

---

# 11. Payload Strategy

Tool Input / Output 必须与全局：

```python
Observability.init(
    payload_strategy="..."
)
```

保持一致。

---

## 11.1 `off`

```text
input 不保存
output 不保存
```

只保存：

```text
tool.name
tool.type
duration
status
input/output type
必要的 size metadata
```

---

## 11.2 `metadata_only`

禁止保存真实值。

例如 Input：

```python
{
    "query": "secret text",
    "limit": 10
}
```

可保存：

```json
{
  "type": "dict",
  "keys": ["query", "limit"],
  "field_count": 2
}
```

不要保存：

```text
secret text
```

Output 同理。

---

## 11.3 `masked`

保存内容，但必须复用 Phase 2.1 已冻结的统一 Masking：

```text
SENSITIVE_KEYS
SENSITIVE_REGEX_PATTERNS
```

例如：

```text
Authorization
api_key
access_token
refresh_token
Bearer ...
sk-...
password
secret
cookie
```

必须一致脱敏。

禁止 Tool 自己维护第三套 Masking 规则。

---

## 11.4 `full`

允许保存完整可序列化内容。

但：

```text
仍然必须执行 Size Guard
```

`full` 不等于：

```text
无限大小
原始二进制
```

---

# 12. Safe Serialization

Tool 输入输出可能不是 JSON。

必须提供统一：

```python
safe_serialize(value)
```

推荐规则：

### Primitive

```text
str/int/float/bool/None
→ 原值
```

### dict/list/tuple

递归处理。

### Dataclass

```python
dataclasses.asdict()
```

### Pydantic

优先：

```python
model_dump()
```

### bytes / bytearray

禁止直接 inline 原始二进制。

保存：

```json
{
  "_type": "bytes",
  "size_bytes": 123456
}
```

### File / Socket / Generator / Unknown Object

禁止深度遍历对象内部。

使用受控：

```text
type name
safe repr preview
```

例如：

```json
{
  "_type": "File",
  "_repr": "<File ...>"
}
```

`repr` 必须继续经过 Masking。

---

# 13. Size Guard

Tool 可能产生：

```text
几 MB 搜索结果
数据库大查询
完整文件
终端日志
```

不能直接塞进 Span。

Phase 2.2 建议冻结默认：

```text
每个 input 最大 32 KiB
每个 output 最大 32 KiB
```

允许后续配置。

若序列化后超过限制：

```json
{
  "_truncated": true,
  "_original_size_bytes": 928371,
  "_preview": "..."
}
```

顺序必须：

```text
safe serialize
↓
mask
↓
size guard / preview
```

不能：

```text
先截取 raw secret
再 mask
```

否则 preview 可能泄露敏感数据。

---

# 14. ToolContextManager 生命周期

进入：

```python
with Observability.tool(...):
```

执行：

```text
1. 校验 SDK initialized
2. 获取 current SpanContext
3. current == None → RuntimeError
4. 生成新的 span_id
5. trace_id = current.trace_id
6. parent_span_id = current.span_id
7. sampled = current.sampled
8. span_kind = TOOL
9. set_context(TOOL)
10. start span
```

退出：

```text
正常：
status = OK

异常：
status = ERROR
error_type
error_message

然后：
end span
report if sampled
restore parent Context
return False
```

异常必须继续抛给业务调用方。

---

# 15. Fail-open 要求

Instrumentation 错误不得改变 Tool 业务行为。

例如：

```text
Payload serialization failed
Reporter failed
Masking failed
Telemetry queue full
```

不能让：

```python
result = web_search(...)
```

失败。

正确：

```text
Tool 业务逻辑成功
Telemetry 尽最大努力记录
Telemetry 失败只记 SDK log
```

但以下属于 API 使用错误，可以明确抛出：

```text
Observability.init() 未调用
Observability.tool() 不在 active Trace 内
name 为空
```

---

# 16. Tool Handle API

`with` 返回 Tool Handle：

```python
with Observability.tool(...) as tool:
    ...
```

至少支持：

```python
tool.set_output(value)
tool.set_attribute(key, value)
tool.add_event(name, attributes=None)
tool.set_error(error_type, error_message)
```

---

## `set_output`

```python
result = search(...)

tool.set_output(result)
```

仅记录 telemetry，不改变 result。

---

## `set_error`

用于业务返回失败但没有抛 Exception：

```python
with Observability.tool(...) as tool:
    result = call_remote()

    if not result.ok:
        tool.set_error(
            "ToolBusinessError",
            result.message,
        )
```

最终：

```text
TOOL status=ERROR
```

但不会自动 raise。

---

# 17. Sampling

Tool Span 必须继承：

```text
current_context.sampled
```

不能再次随机采样。

正确：

```text
AGENT sampled=True
→ TOOL True
→ Tool 内 LLM True

AGENT sampled=False
→ TOOL False
→ Tool 内 LLM False
→ Proxy GATEWAY False
```

Unsampled Tool：

```text
仍创建 Context
仍生成 span_id
仍允许 Child Span 正确传播
```

但：

```text
不序列化大 Payload
不 enqueue telemetry
```

尽量减少采样关闭时的开销。

---

# 18. Async Tool

必须支持：

```python
async with Observability.tool(
    name="http_fetch",
    input={"url": "..."},
) as tool:
    result = await fetch(...)

    tool.set_output(result)
```

实现可直接复用同步生命周期：

```python
__aenter__()
__aexit__()
```

但必须保证：

```text
ContextVar token 在同一个 asyncio Context 中 set/reset
```

---

# 19. 并发语义

代码：

```python
with Observability.tool(name="parallel_search"):

    await asyncio.gather(
        llm_call_1(),
        llm_call_2(),
    )
```

正确：

```text
TOOL parallel_search
├── LLM #1
│   └── GATEWAY
└── LLM #2
    └── GATEWAY
```

不得：

```text
TOOL
└── LLM #1
    └── LLM #2
```

ContextVar 必须继续保持并发隔离。

---

# 20. Thread / Process 边界

Phase 2.2 不承诺：

```text
threading.Thread
ThreadPoolExecutor
ProcessPoolExecutor
subprocess
```

自动继承 ContextVar。

例如：

```python
with Observability.tool(...):
    executor.submit(llm_call)
```

子线程可能没有当前 Trace Context。

此问题明确作为后续：

```text
Context Propagation Helper
```

处理。

不要在 Phase 2.2 顺手实现线程/进程传播。

---

# 21. Tool Decorator

Phase 2.2 增加：

```python
@Observability.instrument_tool(
    name="web_search",
    tool_type="search",
)
def web_search(query: str):
    ...
```

自动产生：

```text
TOOL web_search
```

自动记录：

```text
函数参数 → input
返回值 → output
异常 → ERROR
```

---

# 22. Decorator 参数捕获

建议使用：

```python
inspect.signature()
```

将参数绑定为：

```json
{
  "query": "...",
  "limit": 10
}
```

默认跳过：

```text
self
cls
```

禁止简单：

```python
repr(args)
```

直接存储。

所有参数必须走：

```text
safe serialization
privacy
size guard
```

---

# 23. Async Decorator

必须识别：

```python
async def
```

例如：

```python
@Observability.instrument_tool(
    name="fetch_url",
    tool_type="http",
)
async def fetch_url(url):
    ...
```

正确：

```text
enter TOOL
await function
set output
exit TOOL
```

不得返回未 await 的 coroutine 后就提前结束 Span。

---

# 24. Decorator 与 Context Manager 的关系

Decorator 必须复用：

```text
Observability.tool()
```

不要重新实现第二套 Tool Span 生命周期。

推荐：

```python
def wrapper(...):
    with Observability.tool(...) as tool:
        result = func(...)
        tool.set_output(result)
        return result
```

Async 同理。

原则：

```text
只有一个 Tool Span lifecycle implementation
```

---

# 25. `call_id` 与 LLM Tool Call

典型 OpenAI Tool Calling：

```text
LLM response
tool_calls:
  id=call_123
  function=web_search
```

业务执行：

```python
with Observability.tool(
    name="web_search",
    call_id="call_123",
):
    ...
```

Span：

```text
tool.call_id = call_123
```

Phase 2.2 仅保存关联标识。

不要：

```text
把 TOOL parent_span_id 强行改成已结束的 LLM Span
```

未来可以通过：

```text
Span Links
LLM Tool Call events
Framework Instrumentation
```

增强关联。

---

# 26. Tool Error 语义

## Exception

```python
with Observability.tool(name="terminal"):
    raise RuntimeError("command failed")
```

结果：

```text
TOOL status=ERROR
error_type=RuntimeError
error_message=command failed
```

异常继续向外抛出。

---

## Tool 返回错误对象

例如：

```python
return {
    "ok": False,
    "error": "not found"
}
```

SDK 不自动猜：

```text
这是 ERROR
```

默认：

```text
Span OK
```

业务需要：

```python
tool.set_error(...)
```

原因：

```text
不同 Tool 的成功/失败协议不同
SDK 不应猜业务语义
```

---

# 27. Trace Status

继续沿用当前 Core 规则：

```text
Trace 中存在 ERROR Span
→ Trace ERROR
```

因此：

```text
TOOL ERROR
→ Trace ERROR
```

Phase 2.2 不重新设计：

```text
handled error
retry recovered
partial success
```

这些可作为后续 Trace Status Policy 优化。

---

# 28. Retry 语义

每次显式 Tool 调用：

```text
= 一个 TOOL Span
```

例如：

```python
for i in range(3):
    with Observability.tool(name="web_search"):
        ...
```

结果：

```text
AGENT
├── TOOL web_search ERROR
├── TOOL web_search ERROR
└── TOOL web_search OK
```

不要复用同一个 span_id。

SDK 不自动识别业务函数内部的隐式 retry。

---

# 29. SDK 代码改造建议

基于当前结构，建议修改：

```text
sdk/python/llm_observability/__init__.py
sdk/python/llm_observability/tracer.py
sdk/python/llm_observability/spans.py
sdk/python/llm_observability/utils/masking.py
```

可新增：

```text
sdk/python/llm_observability/tool.py
```

或者把 `ToolContextManager` 放在：

```text
tracer.py
```

Phase 2.2 不要求为了“优雅”做大范围 SDK 重构。

---

# 30. `Span` 模型扩展

当前 SDK `Span.to_record()` 尚未完整输出 Tool Payload。

建议为通用 Span 增加可选：

```python
payload: dict | None
request_metadata: dict | None
```

并在：

```python
to_record()
```

中输出。

这样：

```text
AGENT
LLM
TOOL
INTERNAL
```

未来都可以复用同一 Canonical Record。

本阶段不要新增 Tool 专属 Ingest Endpoint。

---

# 31. Core 改造

当前 Core 已使用通用 Span Schema。

TOOL 应直接进入现有：

```text
/api/v1/ingest
spans table
Trace Detail
```

原则上不需要新增：

```text
tools table
tool_runs table
```

除非当前数据库实际约束阻止 TOOL 入库，否则不要做 DB Migration。

---

# 32. Trace Summary

建议增加：

```text
tool_call_count
```

例如：

```text
Trace

span_count = 7
llm_call_count = 2
tool_call_count = 2
```

统计：

```sql
COUNT(
  CASE WHEN span_kind='TOOL'
  THEN 1 END
)
```

不要影响：

```text
llm_call_count
tokens
LLM latency
```

---

# 33. Tool Metrics

Metrics Summary 建议新增：

```text
tool_call_count
tool_error_count
tool_error_rate

p50_tool_latency_ms
p95_tool_latency_ms
p99_tool_latency_ms
```

语义：

```text
只统计 span_kind='TOOL'
```

公式：

```text
tool_error_rate =
tool_error_count / tool_call_count * 100
```

Tool Metrics 不参与：

```text
LLM Token
LLM Call Count
LLM TTFT
LLM first_chunk
```

---

# 34. TimeSeries

建议增加：

```text
tool_call_count
tool_error_count
tool_avg_latency_ms
```

必须继续保持：

```text
Summary
和
TimeSeries
```

指标语义一致。

不要出现：

```text
Summary 按 TOOL Span
TimeSeries 按所有 Span
```

这种口径漂移。

---

# 35. Tool Filter

Phase 2.2 MVP 不强制新增完整 Tool Analytics 页面。

可暂不实现：

```text
tool_name filter
tool_type filter
tool_error filter
```

如果本阶段顺手加入 `tool_name` Trace Filter，必须使用与 Model Filter 相同的 Trace-level EXISTS 语义：

```text
Tool 命中
→ 返回完整 Trace
```

不能只返回 TOOL Span。

---

# 36. UI Trace Tree

Trace Detail 必须支持：

```text
AGENT
LLM
TOOL
GATEWAY
```

TOOL 使用独立：

```text
Tag
Icon
Waterfall Color
```

必须清晰区分：

```text
LLM
Tool
Gateway
```

---

# 37. TOOL Span Detail

点击 TOOL 后显示：

```text
Tool Name
Tool Type
Call ID
Status
Duration

Input
Output

Error Type
Error Message

Attributes
Events
```

Payload：

```text
JSON 可折叠
长文本折叠
Truncated badge
Masked badge
```

---

# 38. UI 隐私要求

UI 只能展示：

```text
Core 已经处理后的 payload
```

前端不能：

```text
重新读取原始 Tool Input
绕过 masking
```

`metadata_only` 下：

```text
绝不显示真实 input/output 内容
```

---

# 39. Tool Waterfall 示例

```text
AGENT        █████████████████████████████

LLM #1         ███████
 GATEWAY         █████

TOOL search             ████

LLM #2                       ███████
 GATEWAY                       █████

TOOL terminal                        ███
```

要求：

```text
parent/child indentation 正确
时间轴位置正确
Tool duration 独立显示
```

---

# 40. 自动化测试：SDK Core

必须新增：

```text
test_tool_requires_active_trace
test_tool_child_of_agent
test_nested_tool_parent_child
test_tool_llm_child_relationship
test_tool_exception_error_and_reraise
test_tool_manual_set_error
test_tool_sampling_inherited
test_tool_context_restored
test_tool_reporter_failure_fail_open
```

---

# 41. Tool Parent 测试

代码：

```python
with Observability.trace("task"):
    with Observability.tool("search"):
        ...
```

断言：

```text
Tool.trace_id == Agent.trace_id
Tool.parent_span_id == Agent.span_id
Tool.span_kind == TOOL
```

---

# 42. Tool → LLM 测试

```python
with Observability.trace("task"):
    with Observability.tool("search"):
        client.chat.completions.create(...)
```

断言：

```text
AGENT
└── TOOL
    └── LLM
        └── GATEWAY
```

---

# 43. Normal LLM → Tool 测试

```python
with Observability.trace("task"):
    client.chat.completions.create(...)

    with Observability.tool("search"):
        ...
```

断言：

```text
AGENT
├── LLM
│   └── GATEWAY
└── TOOL
```

特别防止：

```text
TOOL 错挂到已结束 LLM 下
```

---

# 44. Nested Tool 测试

```python
with trace:
    with tool("outer"):
        with tool("inner"):
            ...
```

断言：

```text
inner.parent = outer.span_id
outer.parent = agent.span_id
```

---

# 45. Async 测试

```python
async with Observability.tool("async_tool"):
    await ...
```

断言：

```text
Context 正确恢复
Duration 覆盖 await 时间
Error 正确记录
```

---

# 46. Parallel Child LLM

```python
async with Observability.tool("parallel"):
    await asyncio.gather(
        llm1(),
        llm2(),
    )
```

断言：

```text
LLM1.parent = TOOL
LLM2.parent = TOOL

LLM1 不得是 LLM2 parent
```

---

# 47. Payload 测试

必须覆盖四种策略：

```text
off
metadata_only
masked
full
```

重点：

```text
metadata_only 不泄露真实内容
masked 可以识别 nested secret
bytes 不直接存
超大 output 被 truncate
```

---

# 48. Decorator 测试

Sync：

```python
@Observability.instrument_tool(name="search")
def search(query):
    return {"x": 1}
```

Async：

```python
@Observability.instrument_tool(name="fetch")
async def fetch(url):
    ...
```

断言：

```text
input 自动记录
output 自动记录
异常 ERROR
返回值不改变
```

---

# 49. Core 测试

新增：

```text
TOOL Span ingest
Trace Detail includes TOOL
tool_call_count
tool_error_count
tool_error_rate
tool latency percentiles
TimeSeries tool metrics
```

并回归：

```text
LLM Call Count 不变
Token 不受 TOOL 影响
Trace Filter 返回完整 Span Tree
```

---

# 50. UI 测试

至少验证：

```text
TOOL Tag
TOOL waterfall
TOOL detail
Input/Output rendering
Error rendering
Masked/metadata_only
```

---

# 51. E2E Scenario A：单 Tool

```python
with Observability.trace("tool-demo"):

    with Observability.tool(
        name="calculator",
        input={"a": 1, "b": 2},
    ) as tool:
        result = 3
        tool.set_output(result)
```

UI：

```text
AGENT
└── TOOL calculator
```

---

# 52. E2E Scenario B：Agent Loop

```text
AGENT
├── LLM
│   └── GATEWAY
├── TOOL web_search
├── LLM
│   └── GATEWAY
└── TOOL terminal
```

必须验证：

```text
全部同一个 TraceID
正确 parent_span_id
Tool Payload 可查看
Metrics tool_call_count=2
```

---

# 53. E2E Scenario C：Tool 内 LLM

```text
AGENT
└── TOOL retrieval
    └── LLM
        └── GATEWAY
```

这是 Phase 2.2 必须覆盖的关键结构。

---

# 54. E2E Scenario D：Tool Error

```text
AGENT
├── TOOL terminal ERROR
└── ...
```

断言：

```text
Tool error_type/error_message 正确
业务异常仍然可被业务代码捕获
Trace 状态遵循现有 ERROR 聚合规则
```

---

# 55. E2E Scenario E：Sampling=0

```text
sample_rate=0

AGENT
TOOL
LLM
GATEWAY
```

全部：

```text
不入库
```

业务仍然成功。

防止 Phase 2.2 重新引入：

```text
孤立 TOOL
```

---

# 56. 推荐开发顺序

## Step 1：Tool Context Core

实现：

```text
ToolContextManager
Observability.tool()
Tracer.tool()
```

只做：

```text
Trace Context
Parent
Lifecycle
Error
Sampling
```

先不要做 Payload。

---

## Step 2：Tool Payload

实现：

```text
input
set_output
safe serialization
privacy
size guard
```

---

## Step 3：Async

实现：

```text
async with
async context lifecycle
parallel context tests
```

---

## Step 4：Decorator

实现：

```text
instrument_tool
sync
async
arg binding
```

Decorator 必须复用 Step 1-3。

---

## Step 5：Core

实现：

```text
TOOL ingest
tool_call_count
Tool Metrics
TimeSeries
```

---

## Step 6：UI

实现：

```text
TOOL Tag
Waterfall
Detail
Payload
Error
```

---

## Step 7：Real E2E

跑通：

```text
AGENT
├── LLM → GATEWAY
├── TOOL
└── TOOL → LLM → GATEWAY
```

---

# 57. 推荐文件变更范围

预计主要涉及：

```text
sdk/python/llm_observability/__init__.py
sdk/python/llm_observability/tracer.py
sdk/python/llm_observability/spans.py

可新增：
sdk/python/llm_observability/tool.py

core/models/schemas.py
core/storage/db.py

UI:
现有 Trace Detail / Waterfall / Type 定义相关文件

tests/
tests/test_phase2_2_tool_span.py

real_e2e_test.py
```

不要为了 Tool Span 大规模重构：

```text
Proxy
Foundation Storage
OpenAI Instrumentor
Trace Context implementation
```

除非测试证明必须修改。

---

# 58. 兼容性要求

Phase 2.2 完成后以下必须完全保持：

## 无 SDK

```text
Client
→ Proxy

Trace
└── LLM
```

---

## SDK，无 Tool

```text
Trace
└── AGENT
    └── LLM
        └── GATEWAY
```

---

## SDK + Tool

```text
Trace
└── AGENT
    ├── LLM
    │   └── GATEWAY
    └── TOOL
```

新增 Tool 不得改变旧链路语义。

---

# 59. Definition of Done

Phase 2.2 完成必须满足：

```text
Observability.tool() 可用
```

```text
TOOL 正确继承 TraceID / Parent Span
```

```text
普通 LLM → TOOL 是兄弟 Span
```

```text
TOOL → LLM 正确形成父子 Span
```

```text
Nested Tool 正确
```

```text
Sync / Async Tool 正确
```

```text
Decorator 正确
```

```text
Tool Error 自动记录且不吞异常
```

```text
Tool Payload 遵守 off / metadata_only / masked / full
```

```text
大 Payload 有 Size Guard
```

```text
Bytes/不可序列化对象不会破坏 SDK
```

```text
sample_rate=0 不产生孤立 TOOL
```

```text
Core 正确 ingest TOOL
```

```text
UI 正确展示 TOOL
```

```text
tool_call_count / tool_error / latency 口径正确
```

```text
LLM Metrics / Token Metrics 不受 TOOL 影响
```

```text
Real E2E 展示：
AGENT → LLM/GATEWAY + TOOL
```

全部通过后，才标记：

```text
Phase 2.2 Tool Span
✅ COMPLETE
✅ FROZEN
```

---

# 60. Phase 2.2 完成后的目标形态

```text
用户业务任务

Trace ABC
└── AGENT
    ├── LLM
    │   └── GATEWAY
    │
    ├── TOOL web_search
    │
    ├── LLM
    │   └── GATEWAY
    │
    ├── TOOL retrieval
    │   └── LLM
    │       └── GATEWAY
    │
    └── TOOL terminal
```

此时平台完成从：

```text
LLM Request Observability
```

到：

```text
Agent Task
+
LLM
+
Gateway
+
Tool
```

的第一套完整业务 Trace。

下一阶段再考虑：

```text
Phase 2.3
LangChain / Framework Auto Instrumentation
```

原则：

> **Phase 2.2 先把 Tool Span 语义做稳定，再让框架自动生成这些 Span。**
