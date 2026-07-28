# LLM Observability Phase 2.2 最终正确性修复需求文档

> 适用仓库：`xjy486/llm-observability`  
> 基线提交：`11283ccdb8290150acaa94bc330e4fbd3dd65bd7`  
> 文档类型：Phase 2.2 Final Correctness Closeout  
> 当前状态：Phase 2.2 主体能力和上一轮 Closeout 修复已基本完成，但仍存在 2 个必须修复的正确性问题，以及 3 个建议本轮一并收口的问题。  
> 本轮目标：完成最后一轮修复后，将 `Phase 2.2 Tool Span` 正式标记为 `COMPLETE / FROZEN`。

---

# 1. 问题总览

## 必须修复

```text
P0-1 非字符串 Attribute/Event Key 可能抛异常并泄漏 TOOL Context
P0-2 model filter 下 TimeSeries 会丢失 Tool-only Bucket
```

## 建议本轮收口

```text
P1-1 model + time filter 会统计时间窗口外的 Tool Span
P1-2 Reporter Bad Record Isolation 测试覆盖不完整
P1-3 Safe Serialization 缺少全局复杂度预算
```

## 非阻塞字段语义建议

```text
P2-1 tool.input/output.size_bytes 字段名称仍可能误导
```

---

# 2. P0-1：非字符串 Attribute/Event Key 可能导致异常和 Context 泄漏

## 2.1 当前问题

Tool Attribute 清洗逻辑中存在类似：

```python
key.lower()
```

这隐含假设：

```text
Attribute Key 一定是字符串
```

但 Python 字典允许：

```python
attributes={
    123: "value",
    None: "value",
}
```

以及：

```python
tool.set_attribute(
    123,
    "value",
)
```

这类输入可能触发：

```text
AttributeError:
'int' object has no attribute 'lower'
```

## 2.2 更严重的问题：TOOL Context 可能泄漏

当前 `ToolContextManager.__enter__()` 的大致顺序是：

```text
读取 Parent Context
→ 创建 TOOL SpanContext
→ set_context(TOOL)
→ 创建 Span
→ 处理用户 Attributes
→ 处理 Input Payload
→ span.start()
→ 返回 ToolHandle
```

如果在以下阶段抛出异常：

```text
Attribute Key 处理
Input Payload 处理
用户属性遍历
```

则可能出现：

```text
TOOL Context 已激活
但 __enter__() 尚未成功返回
reset_context() 不会执行
```

后续业务代码可能继续看到这个残留 TOOL Context。

错误结构可能变成：

```text
AGENT
└── TOOL incomplete
    ├── LLM
    └── TOOL
```

而这个 TOOL Span 本身并未正常开始或结束。

---

# 3. P0-1 修复要求

## 3.1 所有 Key 必须规范化

增加：

```python
def normalize_attribute_key(key: Any) -> str:
    try:
        value = str(key)
    except Exception:
        return "<invalid-key>"

    if not value:
        return "<empty-key>"

    return value
```

以下位置统一调用：

```text
Observability.tool(attributes=...)
ToolHandle.set_attribute()
ToolHandle.add_event(..., attributes=...)
```

禁止直接对未知类型调用：

```python
key.lower()
```

## 3.2 Event Name 也必须规范化

用户可能传入：

```python
tool.add_event(
    123,
    {...},
)
```

或超长 Event Name。

要求：

```text
转换为字符串
限制最大长度
执行敏感信息 Masking
```

建议：

```text
Event Name 最大 128 characters
Attribute Key 最大 128 characters
```

## 3.3 Canonical 字段比较应基于规范化后的 Key

流程：

```text
raw key
→ normalize to string
→ lowercase for sensitive-key check
→ reserved-key check
→ sanitize value
→ write Span
```

例如：

```python
normalized_key = normalize_attribute_key(key)

if normalized_key in RESERVED_TOOL_KEYS:
    ...
```

## 3.4 调整 Context 激活顺序

推荐 `__enter__()` 顺序：

```text
1. 校验 Active Trace
2. 校验和规范化 name/tool_type/call_id
3. 创建 Span
4. 清洗用户 Attributes
5. 处理 Input Payload
6. span.start()
7. 创建 TOOL SpanContext
8. set_context(TOOL)
9. 返回 ToolHandle
```

原则：

> 只有确认 Tool Span 已完成初始化、即将进入业务代码时，才激活 TOOL Context。

## 3.5 初始化异常必须恢复 Context

即使仍需提前 `set_context()`，也必须：

```python
token = None

try:
    token = set_context(tool_ctx)
    ...
except Exception:
    if token is not None:
        reset_context(token)
    raise
```

但更推荐：

```text
将 set_context() 放到初始化最后
```

减少恢复路径复杂度。

---

# 4. P0-1 验收测试

必须新增：

```text
test_tool_attribute_integer_key_is_normalized
test_tool_attribute_none_key_is_normalized
test_tool_event_non_string_name_is_normalized
test_tool_event_non_string_attribute_key_is_normalized
test_tool_enter_failure_does_not_leak_context
test_tool_enter_failure_preserves_parent_context
```

核心场景：

```python
with tracer.trace("task"):
    agent_ctx = get_current_context()

    with pytest.raises(...):
        with tracer.tool(
            name="bad",
            attributes={BadKey(): "value"},
        ):
            pass

    assert get_current_context().span_id == agent_ctx.span_id
```

还需断言：

```text
后续 LLM / TOOL Parent 仍为 AGENT
```

---

# 5. P0-2：model filter 下 TimeSeries 丢失 Tool-only Bucket

## 5.1 当前问题

TimeSeries 当前分别查询：

```text
Main Query
→ model-filtered LLM / Trace buckets

Tool Query
→ candidate Trace 中的 Tool buckets
```

之后只遍历：

```text
Main Query 返回的 buckets
```

再从 Tool Map 中取同 Bucket 数据。

## 5.2 错误场景

假设：

```text
10:00:30  LLM model=gpt-4
10:01:20  TOOL web_search
interval=60s
```

查询：

```text
model=gpt-4
```

Main Query：

```text
10:00 bucket
```

Tool Query：

```text
10:01 bucket
```

最终若只遍历 Main Query：

```text
10:01 Bucket 不会出现在返回值中
```

导致：

```text
Summary:
tool_call_count = 1

TimeSeries:
tool_call_count = 0
```

Summary 和 TimeSeries 语义发生漂移。

---

# 6. P0-2 修复要求

## 6.1 合并完整 Bucket 集合

分别建立：

```python
main_by_bucket
tool_by_bucket
```

最终：

```python
all_buckets = sorted(
    set(main_by_bucket.keys())
    | set(tool_by_bucket.keys())
)
```

逐 Bucket 合并：

```python
for bucket in all_buckets:
    main = main_by_bucket.get(bucket, DEFAULT_MAIN)
    tool = tool_by_bucket.get(bucket, DEFAULT_TOOL)

    result.append({
        **main,
        **tool,
    })
```

## 6.2 Tool-only Bucket 默认字段

Tool-only Bucket 仍需返回完整 TimeSeries Contract：

```json
{
  "bucket": 0,
  "trace_count": 0,
  "trace_error_count": 0,
  "llm_call_count": 0,
  "llm_error_count": 0,
  "llm_avg_latency_ms": null,
  "avg_ttft_ms": null,
  "avg_first_chunk_ms": null,
  "span_count": 0,
  "tokens": 0,
  "tool_call_count": 1,
  "tool_error_count": 0,
  "tool_avg_latency_ms": 100
}
```

不要因为该 Bucket 没有 LLM 而删除 Tool 数据。

## 6.3 Trace Count 语义

若 Tool-only Bucket 中存在 Tool Span：

```text
trace_count
```

是否应该计入该 Trace，需要与当前 TimeSeries 定义保持一致。

推荐冻结：

```text
trace_count = 在该 Bucket 内有任意 Span 的 distinct Trace 数
```

因此 Tool-only Bucket：

```text
trace_count >= 1
span_count >= tool_call_count
```

若当前 Main Query 无法提供，应在统一 Bucket 查询或补充查询中计算。

---

# 7. P0-2 验收测试

必须新增：

```text
test_model_filtered_timeseries_keeps_tool_only_bucket
test_tool_only_bucket_has_complete_contract
test_summary_and_timeseries_tool_count_are_consistent
```

场景：

```text
LLM 位于 10:00 Bucket
TOOL 位于 10:01 Bucket
interval=60
model=gpt-4
```

断言：

```text
返回两个 Bucket
10:00 llm_call_count = 1
10:01 tool_call_count = 1
所有 Bucket 字段完整
```

---

# 8. P1-1：model + time filter 会统计窗口外 Tool Span

## 8.1 当前问题

当前 Summary Tool Metrics 在有 model filter 时，逻辑类似：

```sql
WHERE span_kind='TOOL'
AND trace_id IN (
    SELECT trace_id
    FROM spans
    WHERE model=?
    AND start_time BETWEEN ? AND ?
)
```

Candidate Trace 由时间窗口内的 LLM 选出，但外层 TOOL 没有重新应用时间过滤。

## 8.2 错误场景

```text
09:50 TOOL old_tool
10:02 LLM model=gpt-4
```

查询：

```text
time_start=10:00
time_end=10:05
model=gpt-4
```

Trace 因 10:02 的 LLM 入选。

当前可能把：

```text
09:50 的 TOOL
```

也统计进去。

于是：

```text
无 model filter
→ 只统计时间窗口内 Tool

有 model filter
→ 可能统计整个 Trace 历史 Tool
```

时间语义不一致。

---

# 9. P1-1 修复要求

Model 只负责：

```text
Trace qualification
```

Tool Span 自身仍需满足时间窗口：

```sql
WHERE span_kind='TOOL'
AND start_time >= ?
AND start_time <= ?
AND trace_id IN (
    SELECT trace_id
    FROM candidate_traces
)
```

推荐显式拆分：

```text
candidate_trace_conditions
tool_span_conditions
```

避免复用一段 `where_clause` 导致语义混乱。

---

# 10. P1-1 验收测试

新增：

```text
test_model_filter_excludes_tool_outside_time_window
test_model_filter_includes_tool_inside_time_window
test_tool_metrics_time_semantics_same_with_and_without_model
```

---

# 11. P1-2：Reporter Bad Record Isolation 测试覆盖不完整

## 11.1 当前问题

当前测试主要验证：

```python
_record_is_json_safe(good_record) is True
_record_is_json_safe(bad_record) is False
```

但没有真正验证：

```text
调用 Reporter._flush()
Good Records 被发送
Bad Record 被丢弃
Bad Record 不回队
dropped_count 增加
sent_count 正确
```

测试名称表达的是 Batch Isolation，但实际只覆盖 Helper。

---

# 12. P1-2 修复要求

使用 Fake Session / Fake Response：

Queue：

```text
good_record_1
bad_record
good_record_2
```

执行：

```python
await reporter._flush()
```

断言：

```text
HTTP body records = [good_record_1, good_record_2]
dropped_count = 1
sent_count = 2
queue empty
bad_record 不再入队
```

还需覆盖：

```text
HTTP 500 时，只将 good records 放回 Queue
bad record 永远不回 Queue
```

以及：

```text
网络异常时，只重试 good records
```

---

# 13. P1-3：Safe Serialization 缺少全局复杂度预算

## 13.1 当前问题

当前 `max_items=1000` 主要是：

```text
每个容器最多处理 1000 项
```

而不是整个序列化操作的总预算。

例如：

```text
Root list 1000 项
每项是一个 dict
每个 dict 又有 1000 项
```

理论上仍可能处理近百万个节点。

## 13.2 Dataclass / Pydantic 绕过问题

当前若使用：

```python
dataclasses.asdict(value)
```

它会先递归复制完整 Dataclass，然后才交给受限 `safe_serialize()`。

对于：

```text
循环 Dataclass
超大 Dataclass
深度嵌套 Dataclass
```

复杂度保护可能来不及生效。

`model_dump()` 也可能在进入 SDK 限制前生成超大对象。

---

# 14. P1-3 修复要求

## 14.1 引入全局 Serialization Budget

示例：

```python
@dataclass
class SerializationBudget:
    remaining_nodes: int = 5000
    remaining_chars: int = 65536
```

每处理一个节点：

```python
budget.remaining_nodes -= 1
```

预算耗尽：

```json
{
  "_truncated": true,
  "_reason": "global_budget"
}
```

## 14.2 Dataclass 不使用递归 asdict

推荐：

```python
for field in dataclasses.fields(value):
    field_value = getattr(value, field.name)
    result[field.name] = safe_serialize(
        field_value,
        budget=budget,
        ...
    )
```

由 SDK 自己控制递归。

## 14.3 Pydantic 处理

优先尝试：

```text
逐字段读取
```

或确保 `model_dump()` 结果仍受全局 Budget 限制。

无法低成本读取时：

```text
只保存 type + bounded repr
```

也不要为了完整 Payload 牺牲业务稳定性。

---

# 15. P1-3 验收测试

新增：

```text
test_safe_serialize_global_node_budget
test_safe_serialize_nested_wide_structure
test_safe_serialize_circular_dataclass
test_safe_serialize_large_dataclass
test_safe_serialize_large_pydantic_model
```

断言：

```text
执行时间受控
输出大小受控
无 RecursionError
无 MemoryError
结果仍可 JSON 序列化
```

---

# 16. P2-1：size_bytes 字段名称建议明确

## 当前语义

大字符串会先在 `safe_serialize()` 中提前截断，再进入 `apply_size_guard()`。

因此业务原始值：

```text
100000 chars
```

最终可能记录：

```text
tool.output.size_bytes ≈ 32768
```

该字段实际表示：

```text
Safe Serialization 后
Size Guard 前
的 captured size
```

而不是业务原始对象大小。

## 建议

二选一：

### 方案 A：重命名

```text
tool.input.captured_size_bytes
tool.output.captured_size_bytes
```

### 方案 B：同时保留多类字段

```text
tool.output.original_size_bytes
tool.output.captured_size_bytes
tool.output.stored_size_bytes
```

其中原始大小只在低成本可获取时填写：

```text
str
bytes
bytearray
已知长度容器
```

该项不阻塞 Phase 2.2，但应避免指标名称误导。

---

# 17. 推荐实施顺序

## Step 1：Context 与输入安全

```text
1. Key / Event Name 规范化
2. 调整 set_context 顺序
3. __enter__ 异常恢复 Parent Context
```

## Step 2：TimeSeries / Metrics 语义

```text
4. 合并 Main + Tool Bucket
5. 补 Tool-only Bucket
6. 修复 model + time 的外层 Tool 时间过滤
```

## Step 3：Reporter 测试

```text
7. Fake Session 测试真实 Batch Isolation
8. HTTP 失败/网络失败重试 Good Records
```

## Step 4：Serialization 鲁棒性

```text
9. 全局 Budget
10. Dataclass 受控遍历
11. Pydantic 大对象保护
```

---

# 18. 本轮禁止事项

在这些问题修完前，不要开始：

```text
Phase 2.3 LangChain Auto Instrumentation
CrewAI
AutoGen
LlamaIndex
MCP Auto Instrumentation
```

本轮只做 Phase 2.2 最终正确性 Closeout。

统一 Docker 的入口路由问题继续作为独立部署任务处理。

---

# 19. Definition of Done

## Context Safety

```text
任意 Attribute/Event Key 类型
→ 不破坏 Tool
→ 不泄漏 Context
```

```text
Tool __enter__ 失败
→ Parent Context 保持不变
```

## TimeSeries

```text
LLM Bucket 与 Tool Bucket 不同
→ 两个 Bucket 都保留
```

```text
Summary Tool Count
与
TimeSeries Tool Count
在同一过滤条件下语义一致
```

## Time Filter

```text
model + time filter
→ 不统计窗口外 Tool
```

## Reporter

```text
Bad Record
→ 丢弃

Good Records
→ 正常发送
```

```text
坏 Record 不进入无限重试
```

## Serialization

```text
循环、超深、超宽、超大对象
→ 有全局预算
→ 不阻塞业务
→ 输出可 JSON 序列化
```

全部新增测试及原有 Real E2E 通过后，可正式标记：

```text
Phase 2.2 Tool Span
✅ COMPLETE
✅ FROZEN
```

随后进入：

```text
Phase 2.3 Framework Auto Instrumentation
```
