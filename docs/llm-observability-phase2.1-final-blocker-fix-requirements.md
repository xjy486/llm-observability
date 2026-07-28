# LLM Observability Phase 2.1 最终错误修复需求文档

> 适用仓库：`xjy486/llm-observability`  
> 基线提交：`48bd433df2e88c52a256e042c0998e734defef56`  
> 文档类型：错误修复需求 / Final Closeout Fix Requirements  
> 当前状态：Phase 2.1 主体能力基本完成，但仍存在 2 个必须修复的 Blocker，以及 4 个建议本轮一并收口的问题。  
> 本轮目标：**完成最后一轮正确性与部署兼容性修复，修完后正式冻结 Phase 2.1，再进入 Phase 2.2 Tool Span。**

---

# 1. 问题总览

两个 Blocker：

```text
BLOCKER-1
sample_rate=0 时 Proxy 仍可能上报 GATEWAY Span

BLOCKER-2
Proxy config 运行时跨目录 import SDK privacy_constants，
标准 docker-compose 构建下会导致 Proxy 启动失败
```

本轮顺手处理：

```text
P1-1 unique_users / unique_sessions 统计口径错误
P1-2 ObservedStream context manager 未正确关闭底层 Stream
P1-3 Reporter stop_sync timeout 与 shutdown_timeout 不一致
P1-4 W3C trace_flags sampled 判断不标准
```

---

# 2. BLOCKER-1：sample_rate=0 时 Proxy 仍可能上报 GATEWAY Span

## 2.1 当前问题

SDK 已实现 Root Head Sampling：

```text
sample_rate=0
→ AGENT 不上报
→ LLM 不上报
```

并继续传播：

```text
traceparent flags=00
```

Proxy 也能解析：

```text
trace_flags=00
→ trace_ctx.sampled=False
→ should_sample=False
```

但当前 `should_sample` 主要只控制：

```text
Payload capture
Streaming content accumulation
```

最终仍会调用：

```python
await self._report_telemetry(...)
```

而 `_report_telemetry()` 仍然：

```python
self.reporter.report(record)
```

因此可能产生：

```text
sample_rate=0

AGENT      ×
LLM        ×
GATEWAY    ✓  ❌
```

最终留下孤儿 Span：

```text
GATEWAY
parent_span_id = 一个不存在的 LLM Span
```

## 2.2 正确语义

Sampling 必须贯穿整条 Trace。

```text
sampled=True
→ AGENT ✓
→ LLM ✓
→ GATEWAY ✓

sampled=False
→ AGENT ×
→ LLM ×
→ GATEWAY ×
```

但业务请求必须：

```text
正常转发
正常返回
traceparent 正常向下游传播
```

## 2.3 推荐实现

把 sampling 决策显式传入 telemetry boundary：

```python
await self._report_telemetry(
    ...,
    sampled=should_sample,
)
```

然后：

```python
if not sampled:
    return
```

建议将“是否上报 telemetry”集中在 `_report_telemetry()` 统一控制，避免不同响应路径漏判断。

## 2.4 error_always_capture 规则

建议冻结：

### 有上游 Trace Context

```text
traceparent flags=00
→ 必须尊重上游 sampling decision
→ 即使下游 HTTP 500，也不要单独上报 GATEWAY Span
```

否则会产生：

```text
父 Span 未采样
子 Span 被强制采样
```

### 无上游 Trace

Proxy 自己作为 Root 时：

```text
sample_rate
error_always_capture
```

可以正常生效。

---

# 3. BLOCKER-1：修正 Sampling=0 E2E

当前 E2E 不应继续通过：

```text
session_id 找不到 Trace
```

判断没有 telemetry，因为孤立 GATEWAY 可能没有 session_id。

也不能用：

```python
trace_count >= 3
```

来表示“unchanged”。

正确流程：

```text
before_trace_count
before_span_count
before_llm_call_count

执行 sample_rate=0 请求

after_trace_count
after_span_count
after_llm_call_count
```

严格断言：

```text
after_trace_count    == before_trace_count
after_span_count     == before_span_count
after_llm_call_count == before_llm_call_count
```

同时断言：

```text
业务请求成功
```

新增单测：

```text
test_proxy_does_not_report_inherited_unsampled_trace
```

构造：

```text
traceparent:
00-<traceid>-<spanid>-00
```

断言：

```text
reporter.report call count = 0
upstream request 正常发送
response 正常返回
```

---

# 4. BLOCKER-2：Proxy Docker 运行时依赖 SDK 源码

## 4.1 当前问题

当前为了共享 Masking 常量，新建：

```text
sdk/python/llm_observability/utils/privacy_constants.py
```

Proxy `config.py` 通过：

```python
sys.path.insert(...)
from privacy_constants import SENSITIVE_KEYS
```

直接依赖 SDK 内部源码目录。

完整仓库本地运行时路径存在，所以测试可过。

但 `docker-compose.yml`：

```yaml
proxy:
  build:
    context: ./proxy
```

而 Proxy Dockerfile：

```dockerfile
COPY . .
```

因此镜像里只有 `./proxy` 内容，不包含：

```text
sdk/python/llm_observability/utils/privacy_constants.py
```

标准 Docker 启动时可能：

```text
python main.py
→ import config
→ from privacy_constants import ...
→ ModuleNotFoundError
→ Proxy 启动失败
```

这是部署 Blocker。

## 4.2 设计原则

禁止：

```text
Proxy
运行时依赖
SDK 内部源码目录
```

正确依赖应为：

```text
SDK ----        → shared/common privacy definitions
Proxy --/
```

---

# 5. BLOCKER-2 推荐方案

## 方案 A：正式公共模块，推荐

例如：

```text
common/
└── privacy/
    ├── __init__.py
    └── constants.py
```

统一提供：

```text
SENSITIVE_KEYS
SENSITIVE_REGEX_PATTERNS
```

SDK 和 Proxy 都显式依赖该模块。

Docker build context 可调整为仓库根：

```yaml
proxy:
  build:
    context: .
    dockerfile: proxy/Dockerfile
```

Dockerfile 只 COPY：

```text
proxy/
common/
```

## 方案 B：MVP 临时方案

若不希望修改 Docker build context：

```text
SDK 自己维护一份 constants
Proxy 自己维护一份 constants
```

但必须新增：

```text
Golden Privacy Contract Tests
```

同一组输入分别跑 SDK / Proxy，断言输出一致。

长期仍建议迁移到正式 common module。

---

# 6. BLOCKER-2：Regex 也必须统一

当前共享文件虽然定义：

```text
SENSITIVE_REGEX_PATTERNS
```

但 Proxy 仍维护自己的：

```text
config.mask_patterns
```

因此实际只是：

```text
Sensitive Keys 基本统一
Regex Patterns 仍然两套
```

必须做到：

```text
Key rules
Regex rules
```

都来自同一来源，或者至少由同一 Golden Contract Test 保证行为一致。

---

# 7. BLOCKER-2 必须新增 Docker Smoke Test

增加：

```text
docker compose build proxy
docker compose up -d core proxy
```

断言：

```text
Proxy 正常启动
GET /health = 200
```

不能只依赖：

```text
pytest
real_e2e_test.py
```

因为它们在完整仓库源码环境运行，覆盖不到 Docker Build Context 问题。

---

# 8. P1-1：unique_users / unique_sessions 统计口径错误

## 当前问题

Session/User filter 已改成 Trace-level，这是对的。

但最终：

```sql
COUNT(DISTINCT user_id)
COUNT(DISTINCT session_id)
```

仍是在 LLM Span 上统计。

SDK 模式通常：

```text
AGENT
  user_id=U1
  session_id=S1

LLM
  user_id=NULL
  session_id=NULL
```

因此可能：

```text
trace_count = 10
llm_call_count = 30
unique_users = 0       ❌
unique_sessions = 0    ❌
```

## 正确规则

Unique User/Session 必须按 Trace-level metadata 统计。

建议：

```sql
WITH trace_dims AS (
    SELECT
        trace_id,
        COALESCE(
            MAX(CASE WHEN span_kind='AGENT' THEN user_id END),
            MAX(user_id)
        ) AS trace_user_id,
        COALESCE(
            MAX(CASE WHEN span_kind='AGENT' THEN session_id END),
            MAX(session_id)
        ) AS trace_session_id
    FROM spans
    GROUP BY trace_id
)
```

然后：

```sql
COUNT(DISTINCT trace_user_id)
COUNT(DISTINCT trace_session_id)
```

测试：

```text
Trace A: U1 / S1, 2 个 LLM
Trace B: U2 / S2, 1 个 LLM
```

断言：

```text
trace_count = 2
llm_call_count = 3
unique_users = 2
unique_sessions = 2
```

---

# 9. P1-2：ObservedStream Context Manager 未正确关闭底层 Stream

当前：

```python
def __enter__(self):
    return self

def __exit__(...):
    self._finalize()
```

只 finalize Observability Span，没有保证底层 OpenAI Stream：

```text
__exit__()
或
close()
```

被调用。

可能出现：

```text
Span 已结束
底层 HTTP Stream / connection 未释放
```

推荐：

```python
def __enter__(self):
    if hasattr(self._stream, "__enter__"):
        self._stream.__enter__()
    return self

def __exit__(self, exc_type, exc_val, exc_tb):
    try:
        if hasattr(self._stream, "__exit__"):
            self._stream.__exit__(exc_type, exc_val, exc_tb)
        elif hasattr(self._stream, "close"):
            self._stream.close()
    finally:
        self._finalize(error=exc_val if exc_type else None)
    return False
```

要求：

```text
underlying stream 正确关闭
Span finalize
report 仅一次
```

---

# 10. P1-3：Reporter stop_sync timeout 与 shutdown_timeout 不一致

当前：

```python
future.result(timeout=10.0)
```

是固定 10 秒。

Reporter 内部又有：

```text
shutdown_timeout
HTTP timeout
```

极端情况下外层固定 timeout 可能先于内部 shutdown 完整结束。

修复：

```python
wait_timeout = self.shutdown_timeout + grace_period
```

例如：

```text
shutdown_timeout + 2s
```

不要硬编码和内部 shutdown timeout 相同的值。

测试：

```text
shutdown_timeout=1
```

确保：

```text
stop_sync 能在合理 grace 内返回
不会过早 stop event loop
```

---

# 11. P1-4：W3C trace_flags sampled 判断不标准

当前：

```python
return self.trace_flags == "01"
```

但 W3C `trace-flags` 是 bit field。

Sampled 标记是最低位 bit 0。

因此：

```text
00 → False
01 → True
02 → False
03 → True
ff → True
```

正确实现：

```python
@property
def sampled(self) -> bool:
    try:
        return bool(int(self.trace_flags, 16) & 0x01)
    except ValueError:
        return False
```

Proxy 是边界组件，必须正确兼容外部 W3C Trace Context。

---

# 12. 自动化测试总要求

本轮必须新增/修正：

```text
1. sample_rate=0 时 Proxy 不 report GATEWAY
2. Sampling=0 Real E2E 使用 before/after count 严格相等
3. docker compose build + proxy health smoke test
4. SDK / Proxy privacy golden contract
5. unique_users / unique_sessions Trace-level 聚合
6. ObservedStream context manager 关闭 underlying stream
7. Reporter stop_sync timeout/grace
8. W3C trace_flags bit semantics
```

---

# 13. 推荐实施顺序

## Phase A：Blockers

```text
1. Proxy Sampling telemetry report gate
2. 修正 Sampling=0 E2E 假阳性
3. 修复 Privacy shared module / Docker build
4. Docker Smoke Test
```

## Phase B：小型正确性修复

```text
5. unique_users / unique_sessions
6. ObservedStream resource close
7. stop_sync timeout alignment
8. W3C trace_flags bit semantics
```

## Phase C：最终验证

```text
9. 全量 pytest
10. Real E2E
11. Docker Compose Smoke
12. 更新 README / CLAUDE.md
```

---

# 14. 本轮禁止事项

在本轮完成前不要开始：

```text
Tool Span
LangChain
AsyncOpenAI
AzureOpenAI
CrewAI
AutoGen
LlamaIndex
OTLP Collector
Gateway Native Instrumentation
```

不要再扩大 Phase 2.1 范围。

---

# 15. Definition of Done

## Sampling

```text
sample_rate=0
→ AGENT 0
→ LLM 0
→ GATEWAY 0
→ 业务请求正常
```

## Docker

```text
docker compose build proxy
→ 成功

docker compose up
→ Proxy 正常启动

/health
→ 200
```

## Privacy

```text
SDK / Proxy
Sensitive Keys 一致
Regex 行为一致
```

## Metrics

```text
unique_users
unique_sessions
按 Trace-level metadata 正确统计
```

## Streaming Resource

```text
with stream
→ underlying stream 正常 close
→ Span 正常 finalize
```

## Reporter

```text
stop_sync
→ 等待时间与 shutdown_timeout 对齐
```

## W3C

```text
trace_flags
→ 按 bit 0 判断 sampled
```

全部新增测试、Real E2E、Docker Smoke 通过后，才允许正式标记：

```text
Phase 2.1 Application SDK & Agent Trace
✅ COMPLETE
✅ FROZEN
```

随后进入：

```text
Phase 2.2 Tool Span
```
