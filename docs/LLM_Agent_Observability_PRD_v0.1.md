# LLM / Agent 可观测平台

**产品需求文档（PRD）**

版本：v0.1  |  状态：Draft  |  面向：MVP 评审

| 产品定位 | 独立于 LLM Gateway 的 LLM / Agent Observability 平台 |
| --- | --- |
| 首个适配对象 | One-API（不修改 Core 的前提下完成基础接入） |
| 公共数据标准 | OpenTelemetry / OTLP + GenAI 语义模型 |
| MVP 核心能力 | Trace / Span、延迟、TTFT、Token、输入输出、错误、查询与基础指标 |
| 核心原则 | 有上游 Trace Context 则继承业务 Trace；无上游 Context 则单次 LLM 请求自动建 Trace 兜底 |

基于 One-API、OpenTelemetry 与 AgentLens 产品形态讨论整理

# 0. 文档说明

| 项目 | 内容 |
| --- | --- |
| 文档目的 | 冻结当前产品目标、范围、核心数据语义与 MVP 验收口径，为后续技术设计与任务拆解提供统一依据。 |
| 当前版本 | v0.1：产品方案已可进入 MVP 评审；具体存储选型、部署拓扑、代码埋点位置等在 Technical Design 中确定。 |
| 主要参考 | One-API 当前网关能力；AgentLens 的链路查询 UI 与 zhiyanllm SDK 接入形态；OpenTelemetry 的 Trace/Span/Context 思想。 |
| 本版假设 | One-API 是首个适配网关，但平台核心不得依赖 One-API 内部对象或数据库结构。 |

## 0.1 决策摘要

> **核心产品决策**
>
> 平台以“独立 Observability Core + 多种采集入口”为长期架构。MVP 首先通过独立 Telemetry Proxy / Adapter 接入 One-API，避免修改 One-API Core；后续增加 Application SDK 自动插桩与 Gateway Native Instrumentation。所有采集方式统一输出 Trace/Span 语义。

- One-API 只是首个数据源，不是平台核心；未来切换 LiteLLM 时，Observability Core 与 UI 不应重写。
- Trace 采用“业务 Trace 优先、单请求 Trace 兜底”：有合法上游 Trace Context 时继承；没有时自动创建新 Trace。
- SessionID / UserID 只用于关联与筛选，不得据此猜测多个请求属于同一个 Trace。
- Prompt / Response 采集必须可配置并支持脱敏；大 Payload 与 Span 元数据逻辑分离。
- MVP 聚焦“看清一次 LLM 调用发生了什么”，不扩张为全套 APM、评测或成本优化平台。
## 0.2 目录

1. 背景与问题定义

2. 产品定位、目标与非目标

3. 用户角色与核心场景

4. 产品边界与总体架构

5. Trace / Span 核心语义

6. MVP 范围与版本路线

7. 功能需求

8. 数据模型与字段要求

9. 采集与兼容性要求

10. 数据安全与隐私

11. 非功能需求

12. 验收标准

13. 风险、TBD 与后续技术设计输入

# 1. 背景与问题定义

## 1.1 背景

One-API 等 LLM Gateway 解决了多模型统一接入、渠道管理、路由、额度与基础调用日志问题，但在开发和运维 LLM / Agent 应用时，仍缺少面向“单次调用链”的可观测能力。开发者通常只能看到一条请求最终成功或失败，以及粗粒度耗时和 Token 信息，无法快速回答一次任务内部经历了哪些 LLM 调用、工具调用、重试或异常。

AgentLens 类产品展示了更贴近 LLM / Agent 开发者的观测形态：以 Trace 表示一次业务任务，以 Span 表示 LLM、Tool、Agent 等子调用，并提供输入输出、耗时、Token、错误、Session 和用户等维度查询。

## 1.2 当前主要问题

| 问题 | 当前表现 | 影响 |
| --- | --- | --- |
| 请求级信息碎片化 | 网关日志、应用日志、模型响应分散 | 定位一次调用问题需要跨多个系统人工拼接 |
| 缺少业务链路视角 | 只能看到单次 LLM API 请求 | 无法理解一个 Agent 任务中的多次 LLM / Tool 调用关系 |
| 延迟不可解释 | 只有总耗时 | 无法区分 TTFT、生成耗时、重试、路由或上游 Provider 延迟 |
| 网关耦合风险 | 若直接把观测能力写入 One-API | 未来升级 One-API 或切换 LiteLLM 时维护成本高 |
| Prompt / Response 难治理 | 要么不保存，要么直接写日志 | 缺乏统一脱敏、采样、权限和保留策略 |

## 1.3 产品机会

> **机会点**
>
> 将“观测平台”从具体 Gateway 中剥离，建立统一的 LLM Telemetry 数据层：应用、Proxy、One-API、LiteLLM 等都可以通过标准 Trace/Span 模型上报；平台只理解统一语义，而不理解某个网关的内部 Go 对象。

# 2. 产品定位、目标与非目标

## 2.1 产品定位

LLM / Agent Observability Platform 是一套独立于 LLM Gateway 的可观测平台，用于采集、关联、查询和展示 LLM / Agent 调用链。平台面向开发者与运维人员，提供从业务任务到单次模型请求的 Trace/Span 视图。

```text
Application / Agent
        │
        ├── Application SDK（业务语义，可选）
        │
        ▼
Telemetry Proxy / Adapter（MVP）
        │
        ▼
One-API / LiteLLM / 其他 Gateway
        │
        ▼
LLM Provider

以上各采集点统一输出 Telemetry
        │
        ▼
OTel Collector / Ingestion
        │
        ▼
Observability Core → Query API → Web UI
```

## 2.2 产品目标

| 目标 ID | 目标 | 成功表现 |
| --- | --- | --- |
| G1 | 看清单次 LLM 调用 | 可查看 Model、输入输出、Token、总延迟、TTFT、状态与错误 |
| G2 | 看清业务任务调用链 | 存在上游 Trace 时，可将 Agent / Tool / LLM 等 Span 串成一个 Trace |
| G3 | 与 Gateway 解耦 | One-API 切换为 LiteLLM 后，核心存储、查询与 UI 无需重写 |
| G4 | 低侵入接入 | MVP 不要求修改 One-API Core；通过独立 Proxy / Adapter 即可工作 |
| G5 | 为深度观测留扩展口 | 未来可增加 Gateway 内部 Routing / Retry / Channel / Fallback Span |

## 2.3 非目标（MVP 不做）

- 不做完整 APM：不覆盖 JVM/CPU/数据库/主机等通用基础设施监控。
- 不做 Prompt Evaluation、幻觉检测、自动评分、模型质量排行榜。
- 不做复杂告警中心、SLA 编排与 Incident Management。
- 不做模型成本优化、自动路由策略推荐。
- 不要求一期就采集 One-API 内部的 Auth、Routing、Retry、Channel Selection 等深层 Span。
- 不支持一期所有 OpenAI 兼容接口；优先聚焦 /v1/chat/completions。
# 3. 用户角色与核心场景

| 用户角色 | 核心诉求 | 典型问题 |
| --- | --- | --- |
| LLM 应用开发者 | 定位一次模型调用是否慢、是否报错、输入输出是否正确 | “这次请求为什么 20 秒？”“到底发给模型了什么？” |
| Agent 开发者 | 理解一次任务中多次 LLM / Tool 调用顺序与耗时 | “是哪一步 Tool 或 LLM 卡住？” |
| Gateway 运维人员 | 分析模型、用户、渠道的调用趋势与异常 | “P99 为何升高？”“错误集中在哪个模型？” |
| 平台管理员 | 管理数据保留、隐私和访问权限 | “哪些 Payload 可以保存？保留多久？” |

## 3.1 核心用户场景

1. 开发者在 Trace 列表中按时间、状态、Model、SessionID、UserID 筛选异常请求。

2. 点击某个 Trace，查看该任务的 Span Tree、总耗时、LLM 调用次数与总 Token。

3. 展开某个 LLM Span，查看请求模型、Prompt / Messages、Tools、响应、TTFT、Token 与错误。

4. 当上游 Agent 已创建 Trace 时，将多个 LLM / Tool Span 聚合在同一个业务 Trace 下。

5. 当没有上游 Trace 时，系统仍自动把每次 LLM 请求记录为可查询的独立 Trace。

6. 未来切换 One-API 为 LiteLLM，继续向同一 Observability 平台上报，查询页面和数据模型保持稳定。

# 4. 产品边界与总体架构

## 4.1 长期架构：三类采集入口，一个核心平台

| 采集入口 | 职责 | 适用阶段 | 能看到什么 |
| --- | --- | --- | --- |
| Application SDK | 自动插桩 OpenAI/Azure/LangChain/Agent；创建业务 Root Trace；传播 Context | P1 | Agent、Chain、Tool、LLM 的业务语义 |
| Telemetry Proxy / Adapter | 透明代理 LLM API；采集请求响应、TTFT、Token；继承或创建 Trace | MVP / P0 | LLM 请求级基础观测 |
| Gateway Native Instrumentation | 在网关内部创建 Routing/Retry/Channel/Provider Span | P2 | 网关内部深度语义 |

## 4.2 核心系统边界

```text
[采集侧]
Application SDK ─┐
Telemetry Proxy ──┼── OTLP / Unified Telemetry ──► [Observability Core]
Gateway Native ───┘                                  │
                                                     ├─ Ingestion / Normalizer
                                                     ├─ Trace & Span Store
                                                     ├─ Metrics Aggregation
                                                     ├─ Payload Store
                                                     ├─ Query API
                                                     └─ Web UI
```

> **边界原则**
>
> Observability Core 不直接依赖 One-API 的数据库表、GORM Model、路由对象或 Channel 实现。所有网关差异应在采集端或 Normalizer 中转换为统一语义。

## 4.3 One-API MVP 接入边界

MVP 优先采用独立 Telemetry Proxy / Adapter，部署在业务客户端与 One-API 之间：

```text
Client / Agent
     │  OpenAI-compatible API
     ▼
Telemetry Proxy / Adapter
     │  transparent forward
     ▼
One-API
     ▼
LLM Provider

Proxy 同时旁路上报 Telemetry → Observability Platform
```

- 不修改 One-API Core 即可获得请求级 Trace、Latency、TTFT、Token、Prompt/Response、Error 等基础观测。
- Proxy 看不到 One-API 内部真实 Routing、Retry、Channel Selection 等细节；此能力归入后续 Native Instrumentation。
- 切换 LiteLLM 时原则上只修改 upstream 地址或适配器配置，Observability Core 不变。
# 5. Trace / Span 核心语义

## 5.1 核心定义

| 对象 | 定义 | 示例 |
| --- | --- | --- |
| Session | 一段连续会话或长期上下文，用于关联多个业务任务 | 同一个用户连续多轮 Agent 对话 |
| Trace | 一次完整业务任务；若无业务上下文，则退化为一次 LLM 请求 | “撤销两个文件并提交”这一任务 |
| Span | Trace 内一个可计时的操作单元 | AGENT、LLM、TOOL、Gateway、Provider Request |
| Event | Span 内瞬时事件 | First Token、Retry、Tool Call Start、Exception |
| Payload | 可能较大的输入输出内容 | system、messages、tools、response |

## 5.2 Trace 创建与传播规则（P0）

> **规则：业务 Trace 优先，单请求 Trace 兜底**
>
> 若请求携带合法上游 Trace Context，则继承该 Trace 并创建子 Span；若不存在有效 Trace Context，则自动创建新的 Trace，以当前 LLM 请求作为根调用进行记录。

```text
收到一次 LLM 调用
       │
       ▼
是否存在合法上游 Trace Context？
       │
   ┌───┴───┐
  YES      NO
   │        │
   ▼        ▼
继承 Trace  自动创建 Trace
   │        │
创建 Child  记录本次 LLM 请求
Span        为 Root/首个 Span
   │        │
   └───┬────┘
       ▼
统一上报 Observability
```

## 5.3 禁止基于 Session 猜测 Trace

SessionID、UserID、时间邻近性仅用于关联与筛选，不得用于判断多个请求属于同一 Trace。即使多个请求 SessionID 相同，只要没有上游 Trace Context，也应分别创建独立 Trace，避免产生持续数小时或跨业务任务的错误 Trace。

## 5.4 典型 Trace 示例

```text
Trace：撤销两个文件并提交
└── chat.AGENT                         26.43s
    ├── llm.completion                  7.67s
    ├── terminal.TOOL                  903ms
    ├── llm.completion                  5.06s
    ├── terminal.TOOL                  739ms
    ├── llm.completion                  7.01s
    ├── terminal.TOOL                    4.0s
    └── llm.completion                  5.25s
```

未来增加 Gateway Native Instrumentation 后，某个 LLM Span 可继续展开为 Gateway → Routing → Provider Request → Retry 等子 Span。

# 6. MVP 范围与版本路线

## 6.1 MVP（P0）范围

| 模块 | MVP 要求 |
| --- | --- |
| 接入 | One-API 前置 Telemetry Proxy / Adapter；不修改 One-API Core |
| API | 优先支持 /v1/chat/completions：非流式 + Streaming SSE |
| Trace | 继承上游 W3C Trace Context；无上游时自动创建 Trace |
| LLM Span | 记录 Model、状态、开始/结束、总延迟、TTFT、Token、错误、Stream 标识 |
| Payload | 支持 Prompt/Messages/Tools/Response 可配置采集、脱敏与按需查看 |
| 查询 | Trace 列表、Trace 详情、Span 列表/树、筛选与详情展开 |
| 指标 | 请求数、错误数/错误率、P50、P95/P99、Token、平均/分位 TTFT |
| 关联 | SessionID、UserID、业务场景等作为属性参与筛选和展示 |

## 6.2 后续版本

| 阶段 | 新增能力 | 价值 |
| --- | --- | --- |
| P1：Application SDK | Python SDK；OpenAI/AzureOpenAI 自动插桩；LangChain/Agent/Tool Context；业务 Root Trace | 实现类似 AgentLens 的业务任务级 Trace |
| P2：Gateway Native | One-API/LiteLLM 内部 Routing、Auth、Retry、Channel、Fallback、Provider Span | 解释“为什么慢/为什么失败”，深入网关黑盒 |
| P3：高级能力 | 告警、成本、质量评测、AI 诊断、更多语言 SDK 与 API 类型 | 从调用观测扩展到完整 LLM Operations |

# 7. 功能需求

## 7.1 Trace 列表页

| 需求 ID | 优先级 | 需求描述 | 验收要点 |
| --- | --- | --- | --- |
| FR-TL-001 | P0 | 展示 Trace 列表 | 每行至少显示状态、输入/输出摘要、开始时间、延迟、SessionID、UserID、Tokens |
| FR-TL-002 | P0 | 时间范围筛选 | 支持指定开始/结束时间，默认展示最近时间窗口 |
| FR-TL-003 | P0 | 多维筛选 | 支持状态、延迟范围、TraceID、SessionID、UserID、Model、自定义标签 |
| FR-TL-004 | P0 | 排序 | 至少支持按开始时间、延迟排序 |
| FR-TL-005 | P1 | Lucene/高级查询 | 支持复杂组合查询表达式 |

## 7.2 Trace 详情页

| 需求 ID | 优先级 | 需求描述 |
| --- | --- | --- |
| FR-TD-001 | P0 | 顶部展示 TraceID、SessionID、UserID、业务场景、开始时间、总延迟、Span 总数、LLM 调用次数、输入/输出/总 Token |
| FR-TD-002 | P0 | 左侧展示 Span Tree 或按时间排序的调用树，区分 AGENT / LLM / TOOL / GATEWAY 类型 |
| FR-TD-003 | P0 | 点击 Span 后在详情区域展示属性、输入输出、事件和错误 |
| FR-TD-004 | P0 | 支持按 Span 名称或 SpanID 搜索 |
| FR-TD-005 | P1 | 支持瀑布时间轴视图，直观看出并行/串行耗时 |

## 7.3 LLM Span 详情

| 需求 ID | 优先级 | 字段/能力 |
| --- | --- | --- |
| FR-LLM-001 | P0 | Model、Provider（若可识别）、Stream、开始时间、结束时间、总延迟 |
| FR-LLM-002 | P0 | TTFT / Time To First Chunk；非流式请求可为空 |
| FR-LLM-003 | P0 | Input Tokens、Output Tokens、Total Tokens；无法获得时明确标记 unknown |
| FR-LLM-004 | P0 | 输入：system / developer / user / tools 等结构化展示 |
| FR-LLM-005 | P0 | 输出：assistant content、tool_calls、finish reason 等结构化展示 |
| FR-LLM-006 | P0 | 错误：HTTP 状态、错误类型、错误消息、异常事件 |
| FR-LLM-007 | P1 | 成本估算、cache token、reasoning token 等扩展字段 |

## 7.4 Dashboard / 概览

| 需求 ID | 优先级 | 指标 |
| --- | --- | --- |
| FR-DB-001 | P0 | 请求数 |
| FR-DB-002 | P0 | 错误数 / 错误率 |
| FR-DB-003 | P0 | P50、P95/P99 总延迟 |
| FR-DB-004 | P0 | 输入、输出、总 Token |
| FR-DB-005 | P0 | TTFT 平均值及 P50/P95（仅有数据时统计） |
| FR-DB-006 | P1 | 按模型、用户、应用、Provider、Gateway 维度聚合趋势 |

## 7.5 Session 与关联查询

- 支持以 SessionID 查看同一会话下多个 Trace，但不得把 Session 自动合并为一个 Trace。
- 支持 UserID、app/service、business.scene、自定义 tags 作为 Trace/Span 属性。
- 支持从 Trace 跳转 Session 视图，查看同一 Session 的时间顺序任务。
# 8. 数据模型与字段要求

## 8.1 统一 Trace 模型

| 字段 | 必选 | 说明 |
| --- | --- | --- |
| trace_id | 是 | 全局唯一 Trace ID；优先继承上游 |
| root_span_id | 是 | Trace 根 Span |
| start_time / end_time | 是 | Trace 时间范围 |
| duration_ms | 是 | Trace 总耗时 |
| session_id | 否 | 会话关联 ID，不参与 Trace 合并判断 |
| user_id | 否 | 终端用户或调用方用户 ID |
| service.name / app_name | 建议 | 应用或服务标识 |
| business.scene | 否 | 业务场景，例如 copilot_all / craft |
| status | 是 | OK / ERROR / UNSET |
| span_count / llm_call_count | 可计算 | 用于列表与详情摘要 |
| token totals | 可计算 | 输入、输出、总 Token 汇总 |

## 8.2 Span 通用字段

| 字段 | 说明 |
| --- | --- |
| span_id / parent_span_id | 构建调用树 |
| span_name | 如 chat.AGENT、llm.completion、terminal.TOOL、gateway.request |
| span_kind | AGENT / LLM / TOOL / GATEWAY / INTERNAL 等 |
| start_time / end_time / duration_ms | 计时信息 |
| status / error | 执行状态与错误 |
| attributes | 统一语义属性 + 少量 vendor 扩展属性 |
| events | First Token、Exception、Retry 等瞬时事件 |
| payload_ref | 指向大 Payload 存储，可为空 |

## 8.3 LLM 语义字段

| 类别 | 字段示例 |
| --- | --- |
| 模型 | gen_ai.provider.name、gen_ai.request.model、gen_ai.response.model |
| Token | gen_ai.usage.input_tokens、gen_ai.usage.output_tokens |
| 会话 | gen_ai.conversation.id 或统一 session.id |
| 时延 | duration_ms、TTFT / time_to_first_chunk |
| 调用 | stream、finish_reason、operation.name |
| 扩展 | llm.gateway.name、llm.gateway.channel.id、llm.retry.index 等 |

> **统一语义原则**
>
> 优先采用 OpenTelemetry / GenAI 语义字段；只有标准无法表达时才增加 llm.gateway.* 等扩展字段。禁止把 oneapi.* 作为平台核心字段命名空间。

## 8.4 Payload 模型

Prompt、Messages、Tools、Response 可能体积大且包含敏感信息，不应强制作为 Span Attribute 全量保存。平台需支持 payload_ref 逻辑，将元数据与大 Payload 分离。

```text
Span Metadata
├── trace_id
├── span_id
├── model
├── tokens
├── latency
├── status
└── payload_ref ─────────► Payload Store
                          ├── system
                          ├── messages
                          ├── tools
                          └── response
```

# 9. 采集与兼容性要求

## 9.1 Telemetry Proxy / Adapter（MVP）

- 对业务侧保持 OpenAI-compatible API 形态，原则上只需修改 Base URL 即可接入。
- 透明转发请求体、Header、Streaming SSE，不应改变业务返回语义。
- 可读取请求与响应中的 Model、Messages、Tools、Usage、错误等信息。
- 流式场景记录首个有效响应 Chunk 时间作为 TTFT；Token 仅在上游返回 usage 时准确记录，否则标记 unknown 或依据明确配置执行估算。
- 必须传播 W3C Trace Context；存在上游 Context 时不得重建 Trace。
- 观测上报失败不得阻塞或破坏主 LLM 请求。
## 9.2 Application SDK（P1）

目标接入形态参考：

```text
from observe import Observe

Observe.init(
    app_name="agent_server",
    api_endpoint="...",
    api_key="..."
)

# 用户继续使用原来的 OpenAI / AzureOpenAI / LangChain 代码
# SDK 自动创建/传播 Trace 与 Span
```

- 支持自动 Instrumentation Registry：OpenAI、AzureOpenAI、LangChain 等按安装情况加载。
- 支持业务层 Root Trace（Agent / Workflow / Chain）与 Tool Span。
- 避免框架 callback 与 SDK 双重插桩导致重复 Span；需要 suppression / dedup 机制。
- SDK 上报地址与 LLM Gateway 地址必须独立配置，形成旁路 Telemetry。
## 9.3 Gateway Native Instrumentation（P2）

- One-API / LiteLLM 可选接入，输出同一 Trace 下的 Gateway 内部 Span。
- 至少覆盖 Auth、Routing、Channel Selection、Provider Request、Retry、Fallback。
- Native 采集不得成为 Observability 平台使用的前置条件。
## 9.4 Gateway 可替换性验收原则

> **关键架构验收**
>
> 将 upstream 从 One-API 替换为 LiteLLM 后，Trace/Span 核心模型、存储、查询 API、Web UI 不需要重写；允许仅调整采集配置或增加薄适配器。

# 10. 数据安全与隐私

| 要求 | MVP 规则 |
| --- | --- |
| Payload 采集策略 | 至少支持 OFF / Metadata Only / Masked / Full 四级策略 |
| 敏感 Header | Authorization、API Key、Cookie 等默认不落库或强制脱敏 |
| Prompt/Response 脱敏 | 支持规则化 Mask；允许按环境、应用或字段配置 |
| 数据保留 | Trace 元数据与 Payload 可配置不同保留周期 |
| 访问控制 | 至少区分普通查看与敏感 Payload 查看权限；MVP 可简化实现但必须预留 |
| 采样 | 支持按比例采样；ERROR Trace 可配置强制保留 |

## 10.1 默认建议

- 开发/测试环境可启用 Masked 或 Full；生产环境默认 Metadata Only 或 Masked。
- Token、密码、Authorization、Secret、Cookie 等内容进入存储前必须脱敏。
- 前端展开完整 Payload 时应有明显的敏感数据提示与权限校验。
# 11. 非功能需求

| NFR | 要求 |
| --- | --- |
| 性能开销 | Proxy / SDK 的观测逻辑不能显著增加主请求时延；异步上报优先 |
| 故障隔离 | Observability Backend 不可用时，LLM 主调用仍应正常完成 |
| 流式兼容 | 不得破坏 SSE 分块、顺序、结束标记与客户端体验 |
| 可扩展性 | Trace/Span Schema 支持自定义 Attributes，避免网关特定字段污染核心模型 |
| 可观测平台自监控 | 至少记录采集失败、队列积压、丢弃数、导出失败等内部指标 |
| 时间准确性 | 统一记录高精度开始/结束时间；TTFT 与总耗时定义必须一致 |
| 数据一致性 | Trace 部分 Span 延迟到达时可增量组装；UI 需能处理未完成 Trace |
| 兼容性 | MVP 至少验证 One-API；设计上不得阻断 LiteLLM 等替换 |

# 12. MVP 验收标准

## 12.1 功能验收

1. 业务将 OpenAI-compatible Base URL 指向 Telemetry Proxy 后，可正常通过 One-API 完成非流式与 Streaming /v1/chat/completions 调用。

2. 每次请求均可在 Trace 列表中找到；无上游 Trace Context 时自动产生独立 Trace。

3. 携带合法 traceparent 时，不重新创建 TraceID，LLM 调用以子 Span 形式进入上游 Trace。

4. Trace 列表可展示状态、开始时间、延迟、SessionID/UserID（若有）、Token 与输入输出摘要。

5. Trace 详情可展示 Span Tree、总耗时、Span 数、LLM 调用次数和 Token 汇总。

6. LLM Span 可查看 Model、输入、输出、总延迟、TTFT、Token、状态与错误。

7. 流式请求首个有效 Chunk 到达时间可被记录为 TTFT；不破坏原始 Streaming。

8. Prompt / Response 按配置执行 OFF / Metadata / Masked / Full 策略。

9. Observability Backend 故障时，主 LLM 调用仍可成功，且不会因上报失败直接返回业务错误。

## 12.2 架构验收

1. One-API Core 无需为 MVP 大面积改造；基础能力通过独立 Proxy / Adapter 获得。

2. 核心 Trace/Span Schema 中不存在必须依赖 One-API 内部对象的字段。

3. 通过替换 upstream 为 LiteLLM 的 PoC，可证明核心 UI/Query/Storage 无需重写。

4. Trace Context 传播逻辑遵循“有上游继承、无上游兜底创建”，SessionID 不参与 Trace 合并。

## 12.3 MVP 成功指标建议

| 指标 | 目标建议 |
| --- | --- |
| Trace 捕获率 | 在测试环境中，对经过 Proxy 的 /v1/chat/completions 请求 ≥ 99% |
| 主请求可用性影响 | 观测后端故障不导致主请求失败 |
| 额外首包开销 | 在同机/同网络基线下保持可接受范围，具体阈值在技术方案压测后冻结 |
| 字段完整率 | 非流式成功请求：Model、总延迟、状态 100%；Token 以 Provider 返回能力为准 |
| Trace 传播正确率 | 携带合法 traceparent 的测试用例 100% 继承正确 TraceID |

# 13. 风险、TBD 与后续技术设计输入

## 13.1 已识别风险

| 风险 | 影响 | 建议 |
| --- | --- | --- |
| Proxy 看不到 Gateway 内部细节 | 只能知道总 LLM 请求结果，难解释 Retry/Channel | 作为 MVP 接受；P2 引入 Native Instrumentation |
| Streaming Token Usage 不总是返回 | Token 统计可能缺失 | 明确 unknown；支持 include_usage 等兼容策略，不静默伪造 |
| Prompt/Response 体积大 | 存储成本、OTLP 截断、查询性能问题 | Metadata 与 Payload 分离；采样与保留策略 |
| 多层自动插桩重复 Span | Trace 树混乱、统计重复 | P1 SDK 设计 suppression/dedup |
| Trace Context 跨组件丢失 | 业务 Trace 被拆成多个孤立 Trace | 端到端验证 W3C Context 传播 |
| 不同 Gateway 字段差异 | UI 出现 vendor 特判 | Normalizer + Canonical Schema，vendor 字段仅做扩展 |

## 13.2 进入 Technical Design 前仍需冻结的 TBD

| TBD | 需要决策的问题 | 建议方向 |
| --- | --- | --- |
| TBD-01 | Proxy 与 Collector 是否合并部署？ | 职责分离：Proxy 做请求观测，Collector 做遥测管道；开发态可合并部署 |
| TBD-02 | Trace Store / Metrics Store / Payload Store 具体选型？ | 在技术方案中按规模、查询模式与现有基础设施评估 |
| TBD-03 | UserID / SessionID 如何注入？ | 定义 Header / Metadata 约定与 SDK Context API；缺失时允许为空 |
| TBD-04 | MVP 是否要求 traceparent 之外支持自定义 Trace Header？ | 标准 W3C 为主，可兼容内部 Header 但最终归一化 |
| TBD-05 | Payload 默认策略是什么？ | 生产建议 Metadata Only/Masked；测试环境可 Full |
| TBD-06 | 是否在 MVP 同时实现最小 Python SDK？ | 若业务 Trace 是首期强需求，则增加“Context-only SDK”；否则放 P1 |
| TBD-07 | UI 是否完全复刻 AgentLens？ | 借鉴信息架构，不做像素级复刻；优先保证查询效率与调用树可读性 |

## 13.3 技术设计文档必须回答的问题

- Proxy 如何透明处理非流式与 SSE Streaming，并准确统计 TTFT？
- 如何从请求/响应提取 Usage、Tool Calls 与错误，同时保持协议兼容？
- Trace Context 在 Client → Proxy → Gateway → Provider/SDK 之间如何传播？
- OTLP 数据如何进入 Collector、Normalizer、Storage，如何处理迟到 Span 与未完成 Trace？
- Trace / Span / Payload 的存储 schema、索引、保留和查询路径如何设计？
- 如何做异步上报、批量、重试、背压、采样与故障隔离？
- 如何在 One-API 与 LiteLLM 上完成兼容性 PoC？
- P1 SDK 自动插桩采用何种机制，如何避免重复 Span？
# 附录 A：MVP 页面信息架构（建议）

```text
首页 / 概览
├── 请求数
├── 错误数 / 错误率
├── P50 / P95 / P99 延迟
├── TTFT
└── Token

链路查询
├── Trace 列表
│   ├── 状态
│   ├── 输入/输出摘要
│   ├── 开始时间
│   ├── 延迟
│   ├── SessionID
│   ├── UserID
│   └── Tokens
│
└── Trace 详情
    ├── Trace 摘要
    ├── Span Tree
    └── Span 详情
        ├── 输入 / 输出
        ├── Attributes
        ├── Events
        ├── Error
        └── Timing / Tokens
```

# 附录 B：最终架构原则清单

| 原则 | 说明 |
| --- | --- |
| P1：平台独立 | Observability 不是 One-API 的一个页面或模块，而是独立产品 |
| P2：标准边界 | 采集端统一输出 Trace/Span；优先 OpenTelemetry / OTLP |
| P3：不猜业务边界 | 有上游 Context 才合并业务 Trace；否则单请求 Trace 兜底 |
| P4：网关可替换 | One-API → LiteLLM 不应导致核心平台重写 |
| P5：基础可用 + 深度增强 | 不改 Gateway 能用；加 Native Instrumentation 后看得更深 |
| P6：Payload 可治理 | 输入输出可配置、可脱敏、可采样、可独立保留 |
| P7：观测不影响业务 | Telemetry 故障不得成为 LLM 主链路单点故障 |

---

—— PRD v0.1 End ——
