# LLM Observability Phase 2.2 Tool Span 修复需求文档

> 适用仓库：`xjy486/llm-observability`  
> 审查范围：从 `bdaa94d98a9657adb933d7ca6652dca5baecfa87` 起及其之后提交  
> 当前状态：Phase 2.2 主体架构与主流程已实现，但仍存在 SDK 可用性、Telemetry 安全性、指标过滤语义和若干正确性问题。  
> 本轮目标：修复 Phase 2.2 Closeout 问题，完成后再标记 `Phase 2.2 COMPLETE / FROZEN`。

---

# 1. 问题总览

```text
P0-1 Public Decorator 无法在 Observability.init() 前定义
P0-2 Tool Attributes / Events 可能污染 Reporter Queue，并绕过隐私规则
P0-3 model filter 下 Tool Metrics 统计错误

P1-1 Unsampled Tool 仍处理完整 Payload
P1-2 set_output(None) 无法区分“未设置输出”
P1-3 tool.input/output.size_bytes 口径错误
P1-4 Tool duration 混入 output telemetry 处理耗时
P1-5 Core Pydantic API Contract 未同步 Tool 字段
P1-6 Safe Serialization 缺少循环引用与复杂度保护
```

另外，统一 Docker 镜像存在独立部署问题，应作为单独部署任务处理，不与 Tool Span Closeout 混在一起。

---

# 2. P0-1：Public Decorator 无法在 init 前定义

## 当前问题

当前写法：

```python
@Observability.instrument_tool(
    name="web_search",
    tool_type="search",
)
def web_search(query: str):
    ...
```

在装饰器定义阶段就检查 SDK 是否已初始化。

但 Python 装饰器通常在模块 import 时执行，真实启动顺序通常是：

```text
import modules
→ decorator 创建 wrapper
→ 应用启动
→ Observability.init()
→ 业务开始运行
```

当前实现反而要求：

```python
Observability.init()

@Observability.instrument_tool(...)
def web_search(...):
    ...
```

这对 FastAPI、Django、CLI 和普通模块化项目都不友好。

## 修复要求

装饰器定义阶段不得要求 SDK 已初始化。

正确流程：

```text
模块 import
→ instrument_tool() 返回 wrapper
→ 不读取 tracer

函数真正执行
→ 检查 Observability 是否已初始化
→ 获取当前 tracer
→ 创建 TOOL Span
```

## 错误语义

定义函数时：

```text
不报错
```

调用函数时若 SDK 未初始化：

```text
RuntimeError:
Observability.init() must be called before invoking an instrumented tool
```

调用函数时若没有 Active Trace：

```text
RuntimeError:
Observability.tool() requires an active trace
```

## 验收测试

```text
test_public_decorator_can_be_defined_before_init
test_public_decorator_raises_on_call_before_init
test_public_decorator_sync_after_init
test_public_decorator_async_after_init
```

---

# 3. P0-2：Tool Attributes / Events 可能污染 Reporter Queue

## 当前问题

以下数据未经统一处理直接进入 Span：

```python
Observability.tool(attributes={...})
tool.set_attribute(key, value)
tool.add_event(name, attributes={...})
```

这些值没有经过：

```text
safe_serialize
masking
size guard
```

## 风险一：不可序列化对象毒化整个 Batch

例如：

```python
tool.set_attribute("client", SomeCustomObject())
```

Reporter 在 `json={"records": batch}` 时可能编码失败，并把整个 Batch 重新放回 Queue。

可能形成：

```text
坏 record 位于队首
→ JSON encode 失败
→ 整个 batch 回队
→ 下次继续失败
→ 正常 Span 也无法发送
```

## 风险二：敏感信息绕过 Masking

例如：

```python
tool.set_attribute("api_key", "sk-secret")
```

或：

```python
tool.add_event(
    "request",
    {"authorization": "Bearer secret"},
)
```

当前可能直接入库。

## 风险三：用户覆盖 Canonical Fields

用户 attributes 可能覆盖：

```text
tool.name
tool.type
tool.call_id
```

导致 `span_name` 与 attributes 冲突。

## 修复要求

### Canonical Key 保护

禁止用户覆盖：

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

推荐：

```text
忽略并记录 warning
```

不要让 Telemetry 配置错误影响业务。

### Attribute / Event 规范化

所有用户数据进入 Span 前必须：

```text
safe_serialize
→ mask
→ size guard
```

建议：

```text
单个 attribute 最大 4 KiB
单个 event attributes 最大 16 KiB
```

### Reporter 最终防线

Reporter 在发送前做单条 JSON preflight。

单条无法序列化：

```text
丢弃该 record
增加 dropped_count
记录 error
继续发送其他正常 record
```

禁止一个坏 record 无限阻塞整个 Queue。

## 验收测试

```text
test_tool_custom_attribute_is_json_safe
test_tool_event_attributes_are_json_safe
test_tool_attribute_secret_is_masked
test_tool_reserved_attribute_cannot_be_overridden
test_bad_record_does_not_poison_reporter_batch
```

---

# 4. P0-3：model filter 下 Tool Metrics 错误

## 当前问题

当前 Tool Metrics 实际类似：

```sql
WHERE model='gpt-4'
AND span_kind='TOOL'
```

但正常 Tool Span：

```text
model = NULL
```

所以：

```text
metrics?model=gpt-4
```

会返回：

```text
tool_call_count = 0
tool_error_count = 0
tool latency = 0
```

即使匹配模型的 Trace 中实际存在 Tool。

## 正确规则

Model 应作为 Trace qualification filter：

```text
第一步
找包含 model=gpt-4 LLM Span 的 candidate Trace IDs

第二步
在 candidate traces 的完整 Span 集合中聚合 TOOL Span
```

## 推荐 SQL

```sql
WITH candidate_traces AS (
    SELECT DISTINCT trace_id
    FROM spans
    WHERE model = ?
)
SELECT
    COUNT(*) AS tool_call_count,
    SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) AS tool_error_count
FROM spans
WHERE span_kind='TOOL'
AND trace_id IN (
    SELECT trace_id FROM candidate_traces
)
```

TimeSeries 同理。

## 验收测试

构造：

```text
Trace A:
LLM model=gpt-4
TOOL count=2

Trace B:
LLM model=claude
TOOL count=3
```

查询：

```text
model=gpt-4
```

断言：

```text
tool_call_count = 2
```

---

# 5. P1-1：Unsampled Tool 仍处理完整 Payload

## 当前问题

Tool 能正确继承 `sampled=False` 并不 report，但仍会执行：

```text
safe_serialize
mask_payload
json.dumps
size guard
```

所以 `sample_rate=0` 时，超大 Tool Payload 仍产生明显 CPU 和内存开销。

## 修复要求

进入 Tool 时保存：

```python
self._sampled = current.sampled
```

如果未采样：

```text
不处理 input payload
不处理 output payload
不构造 request_metadata
不执行 JSON size 计算
```

仍需：

```text
创建 Context
生成 span_id
允许 Child LLM / GATEWAY 继承 sampled=False
恢复 Context
```

## 验收测试

```text
test_unsampled_tool_skips_input_serialization
test_unsampled_tool_skips_output_serialization
```

---

# 6. P1-2：set_output(None) 无法正确记录

## 当前问题

当前通过：

```python
getattr(span, "_tool_output", None)
```

判断是否设置输出。

导致：

```python
tool.set_output(None)
```

和：

```text
从未调用 set_output()
```

无法区分。

## 修复建议

使用 sentinel：

```python
_OUTPUT_UNSET = object()
```

最终区分：

```text
未设置 output
和
output 确实为 None
```

`set_output(None)` 应记录：

```json
{
  "output": null
}
```

并设置：

```text
tool.output.type = NoneType
```

## 验收测试

```text
test_tool_output_none_is_recorded
test_tool_without_set_output_has_no_output_field
test_decorator_return_none_records_null_output
```

---

# 7. P1-3：tool.input/output.size_bytes 口径错误

## 当前问题

Payload 超限后，`size_bytes` 计算的是截断后 wrapper 的大小，而不是原始序列化数据大小。

可能出现：

```text
_original_size_bytes = 100000
tool.output.size_bytes = 600
```

字段语义冲突。

## 修复要求

`apply_size_guard()` 返回：

```python
guarded_data
truncated
original_size_bytes
```

`tool.input.size_bytes` 和 `tool.output.size_bytes` 必须表示：

```text
Masking 后、截断前的原始序列化大小
```

可选新增：

```text
tool.input.stored_size_bytes
tool.output.stored_size_bytes
```

表示实际入库大小。

## 验收测试

```text
test_large_tool_output_size_bytes_is_original_size
test_small_tool_output_size_bytes_matches_serialized_size
```

---

# 8. P1-4：Tool Duration 混入 Output Telemetry 处理耗时

## 当前问题

当前退出流程：

```text
Tool 业务结束
→ 处理 output
→ mask
→ json encode
→ size guard
→ span.end()
```

因此：

```text
Tool duration
=
业务 Tool Runtime
+
输出 Telemetry 处理耗时
```

同时 input 处理又发生在 `span.start()` 前，语义不对称。

## 正确语义

`TOOL duration_ms` 应只表示：

```text
Tool 业务执行时间
```

不应包含：

```text
Telemetry Serialization
Masking
Size Guard
Reporter enqueue
```

## 推荐顺序

进入：

```text
创建 Span
span.start()
处理 input telemetry
执行业务 Tool
```

退出：

```text
设置 status/error
span.end()
处理 output telemetry
设置 request_metadata
report
restore context
```

## 验收测试

构造慢序列化对象，断言 Tool duration 接近业务执行耗时，而不是 telemetry 处理耗时。

---

# 9. P1-5：Core Pydantic API Contract 未同步

## 当前问题

Storage 和 Frontend 已加入：

```text
tool_call_count
tool_error_count
tool_error_rate
p50/p95/p99_tool_latency_ms
```

但 Core Pydantic Models 仍缺少这些字段。

因此：

```text
Storage Return
≠
Core Schema
≠
Frontend Type
```

## 修复要求

### TraceSummary

```python
tool_call_count: int = 0
```

### TraceDetail

```python
tool_call_count: int = 0
```

### MetricsSummary

```python
tool_call_count: int = 0
tool_error_count: int = 0
tool_error_rate: float = 0.0
p50_tool_latency_ms: float = 0.0
p95_tool_latency_ms: float = 0.0
p99_tool_latency_ms: float = 0.0
```

TimeSeries Model 同步：

```text
tool_call_count
tool_error_count
tool_avg_latency_ms
```

## 验收测试

增加 API Contract Test，保证：

```text
Storage keys
Core schema fields
Frontend TypeScript fields
```

三者一致。

---

# 10. P1-6：Safe Serialization 缺少复杂度保护

## 当前问题

当前递归处理 dict/list/dataclass/Pydantic，但没有：

```text
循环引用检测
最大递归深度
最大元素数量
字符串提前裁剪
```

例如：

```python
a = {}
a["self"] = a
```

可能无限递归。

超大列表也会完整遍历后才触发 Size Guard。

## 修复建议

为 `safe_serialize()` 增加：

```python
max_depth=8
max_items=1000
max_string_chars=32768
seen_ids=set()
```

循环引用：

```json
{
  "_type": "circular_reference"
}
```

深度超限：

```json
{
  "_truncated": true,
  "_reason": "max_depth"
}
```

元素超限：

```json
{
  "_truncated": true,
  "_reason": "max_items"
}
```

## 验收测试

```text
test_safe_serialize_circular_reference
test_safe_serialize_max_depth
test_safe_serialize_max_items
test_safe_serialize_large_string
```

---

# 11. 测试要求汇总

至少补充：

```text
1. Public decorator 可在 init 前定义
2. Decorator 调用时才校验 init
3. Attributes / Events JSON-safe
4. Reserved Tool Attributes 不可覆盖
5. 坏 record 不污染 Reporter Batch
6. model filter 下 Tool Metrics 正确
7. model filter 下 Tool TimeSeries 正确
8. Unsampled Tool 跳过 Payload
9. set_output(None) 正确记录
10. size_bytes 使用原始大小
11. Tool duration 不包含 Telemetry 处理耗时
12. Core Pydantic Contract 同步
13. safe_serialize 循环引用与复杂度保护
```

---

# 12. 推荐实施顺序

## Phase A：冻结前必须修复

```text
1. Public Decorator Lazy Initialization
2. Attributes / Events Sanitization
3. Reporter Bad Record Isolation
4. model filter 下 Tool Metrics
```

## Phase B：正确性收口

```text
5. Unsampled Tool 跳过 Payload
6. set_output(None)
7. size_bytes 口径
8. Tool duration 语义
9. Core Schema Contract
```

## Phase C：鲁棒性

```text
10. safe_serialize 循环引用与复杂度保护
11. Regression Tests
12. Real E2E 回归
```

---

# 13. 统一 Docker 提交单独处理

`cbe7e49...` 建议单独建立部署修复任务。

当前需核查：

```text
EXPOSE 8000，但默认无进程监听 8000
UI /api/v1 没有反向代理到 Core
Proxy 默认 UPSTREAM_URL 指向 UI 3000
启动脚本依赖 curl，但镜像未显式安装
```

推荐结构：

```text
Nginx / Caddy :8000
├── /api/v1/* → Core :8001
├── /v1/*     → Proxy :8082
└── /*        → UI static
```

统一镜像修复前不应标记为生产可用。

---

# 14. Definition of Done

```text
Decorator 可在 SDK init 前定义
```

```text
Tool attributes/events 全部 JSON-safe、masked、bounded
```

```text
单个坏 telemetry record 不会阻塞整个 Reporter Queue
```

```text
用户不能覆盖 tool.name / tool.type / tool.call_id
```

```text
model filter 下 Tool Metrics 与 TimeSeries 正确
```

```text
sample_rate=0 不处理 Tool 大 Payload
```

```text
set_output(None) 可正确记录
```

```text
tool.*.size_bytes 语义明确且正确
```

```text
Tool duration 只表示业务 Tool Runtime
```

```text
Core Schema / API / Frontend Types 一致
```

```text
Safe Serialization 能处理循环引用和超大复杂对象
```

全部单测、Real E2E 和 Contract Tests 通过后，再标记：

```text
Phase 2.2 Tool Span
✅ COMPLETE
✅ FROZEN
```

随后进入：

```text
Phase 2.3 Framework Auto Instrumentation
```
