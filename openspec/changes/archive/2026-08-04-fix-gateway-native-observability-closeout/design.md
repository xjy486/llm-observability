# Design: fix-gateway-native-observability-closeout

## Context

Phase 3（commit `6c00760`，审查结论 CHANGES REQUIRED）已搭好 Gateway Contract / Runtime / Adapter / Streaming / Usage / Privacy / Sampling / Fail-open / Registry / Mock E2E 的完整骨架，但核心语义存在 8 个 P0 与 7 个 P1 缺陷（完整清单见 `docs/llm-observability-phase3-rework-bug-fix-requirements.md`）。问题集中在四类：

1. **身份与来源**：无 SDK 时 Router 可能产生 `trace_id=None`；有上游 traceparent 时 `trace_origin` 仍写成 `gateway`。
2. **生命周期**：Attempt 关闭连带清空整个 Gateway Context；Streaming 在响应头阶段就聚合成功；Router finalize 只删 Registry 不结束遗留 Attempt Span。
3. **所有权与隐私**：LLM Usage 依赖同进程 ContextVar 回写（跨进程不成立）；Event 泄露原始 channel ID。
4. **流程**：Real E2E 复用 Phase 2.5 文件且变量不匹配（全 skip 也绿）；CI 用单引号 sed 假脱敏；OpenSpec 提前归档。

约束：Phase 2.1 已冻结，2.1～2.5 全量回归必须通过；禁止修改 One-API 路由行为、禁止新增 SpanKind、禁止吞业务异常、Telemetry 全链路 fail-open；不修改已归档的 change 历史。

## Goals / Non-Goals

**Goals:**

- 修复全部 8 个 P0 与 6 个 P1 代码缺陷（P1-7 由本 change 流程自身解决）。
- 每个修复配对抗性断言测试（父子状态一致性、TraceID 合法性、重复聚合、Registry 残留、Secret 泄漏、必需测试被 skip）。
- 建立真正的 Gateway Real E2E + CI 门禁（0 skipped、Secret 缺失即失败、Python 脚本脱敏）。
- 分三个检查点实施，每个检查点审查通过再进入下一个。

**Non-Goals:**

- 不重写 Phase 3 架构；不改动 Adapter 的提供商覆盖面（One-API / LiteLLM 骨架保持）。
- 不设计 `x-llm-obs-*` 回传协议的签名/信任边界细节（P0-7 只要求删除 ContextVar 强制语义，协议另行设计）。
- 不修改已归档 change 内容；不改动 One-API 路由行为；不新增 SpanKind。
- 客户端取消是否计为 ERROR 的产品语义不在本轮重新定义——但选定后 Router/Attempt 必须一致并冻结。

## Decisions

### D1：显式 `ResolvedGatewayParent` 替代隐式推断（P0-1/P0-2）

Parent Resolver 返回冻结 dataclass `ResolvedGatewayParent{trace_id, parent_span_id, origin, upstream_trace_present}`，`origin ∈ {sdk_context, remote_traceparent, gateway_root}`。Span 属性由该结构直接映射（`sdk/remote/gateway`），禁止从"是否存在 parent 对象"二次推断——这是 P0-2 的根因。无上游时 `generate_trace_id()` 产出 32 位小写十六进制、拒绝全 0（生成后校验，全 0 则重生成）；`upstream_trace_present` 与 origin 严格绑定而非独立赋值。

**备选**：保留现有"尝试解析 context/headers，失败则 None"的隐式流——被否，因为它正是 null TraceID 的来源。

### D2：Context 分槽 `GatewayContextState{router, active_attempt}`（P0-3）

单一 ContextVar 持有一个不可发 `GatewayContextState`（两个槽位），而非每槽一个独立 ContextVar。理由：单次 token 设置/复位保证两个槽位在同一执行上下文里原子快照，cross-context reset 时只需构造"router 不变、attempt 替换"的新 state，天然满足"只清 Attempt 槽"。Attempt close（含异常、async、cross-context reset 路径）一律只产生 `replace(active_attempt=prev)`；`clear_gateway_context()` 仅在 Router 终态调用。

**备选**：两个独立 ContextVar——被否，两个 token 的复位顺序在异常路径下易产生半恢复状态。

### D3：Streaming 终态拆分与一次性聚合（P0-4）

公开 API 拆为 `finish_non_streaming_attempt(...)` 与 `finalize_streaming_attempt(...)`。Streaming 路径在响应头/Wrapper 创建阶段只记录 `gateway.stream.started`，不构造 `AttemptResult`、不聚合、不 end span。Wrapper 持有真实上游请求开始时间戳（Attempt start 时记录，非 Wrapper 创建时），首个"有效内容"判定函数过滤 keepalive/空串/metadata-only/usage-only/`[DONE]` 后才记录 TTFT 与 `gateway.stream.first_token`。终态（完成/错误/取消/超时/`close()`/`aclose()`）统一进入一个幂等的 `_finalize_once(terminal)`：构造最终 `AttemptResult` → 聚合 Router → close Attempt → close Router。取消语义冻结为：Attempt `error_category=client_cancelled`，Router `final_error_category=client_cancelled`，两者状态一致（同为 ERROR 映射）。

**备选**：在 Wrapper 各终止分支分别聚合——被否，分支发散正是 P0-4 重复/不一致聚合的根因；单一 finalize funnel 天然幂等。

### D4：Router 持有 `_open_attempts`，`force_close()` 兜底（P0-5）

Attempt start 注册、close 注销；Router `finalize()` 对快照逐个 `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`。`force_close` 幂等（已关闭直接返回）、不覆盖已有业务错误（仅在无 error_category 时写入）、清 Registry + Context Attempt 槽、上报终态。这同时修复"用 Router span_id 删 Attempt Registry"的键错配——注销键统一为 Attempt 自身标识。

### D5：Usage 所有权三分，删除 ContextVar 回写（P0-7，语义 BREAKING）

Attempt = 单次真实请求 Usage/Cost；Router = 全部 Attempt（含失败）汇总；SDK LLM = 调用方逻辑响应 Usage。删除"LLM Usage 必须等于 Router Aggregate"的同进程回写路径；`instrumentation/openai.py` 与 LangChain `llm_span.py` 中对应 hook 改为仅记录逻辑 Usage。Retry Waste 由 Core/UI 从 Trace 树推导（Router − 最终成功 Attempt），不在 SDK 内物化。若未来需要客户端感知实际消耗，走显式 Header 协议（另行设计签名/信任边界）。

**备选**：保留 ContextVar 回写并标注"仅同进程有效"——被否，它在真实部署（应用进程 ≠ 网关进程）下静默产生错误数据，比没有数据更糟。

### D6：隐私统一入口 `set_gateway_attribute()` + 事件 channel 哈希（P0-8/P1-5）

所有 Router/Attempt 外部字符串属性经唯一入口写入：字段白名单（默认拒绝未知 key）→ 值脱敏（secret 模式、URL query 剥离）→ 长度截断（字符串 ≤512B；request_id/route/reason ≤256B；provider/model ≤128B）→ 类型规范化 → fail-open。Event 侧：`fallback.selected` 冻结为 `from_channel_id/to_channel_id/reason`，所有 `channel_id` 类事件字段统一过 `PrivacyGuard.hash_channel_id()`（HMAC，稳定、可区分）。属性名保持 Contract 原名（不加 `_hash` 后缀），值保证已哈希——避免双命名漂移。

### D7：Attempt Index 由 Router 分配（P1-1）

`router.allocate_attempt_index()`：内部计数器 + 锁（asyncio 与 threading 双路径均安全）。显式合法正整数→采用；重复→重映射到下一可用值并记 warning event；0/负/非整数→回退自动分配。`attempt_count` 由实际 Attempt 数推导而非计数器自增。

### D8：Cost 绑定 resolved model，价格单位冻结 USD/1M tokens（P1-2）

`CostCalculator.calculate(usage, model=attempt.resolved_model)`；配置键冻结为 `input_usd_per_1m_tokens` / `output_usd_per_1m_tokens`；无价格→`cost.source=unpriced`；`handle_cache(..., cost=...)` 显式 cost 优先，未传且有 usage 时按 resolved model 计算。

### D9：Real E2E 全链路与 CI 门禁（P0-6）

新文件 `test_real_gateway_e2e.py`：真实 HTTP 客户端 → middleware/adapter → Runtime → Router/Attempt → mock upstream（可控失败/流式）→ Reporter → 内存 mock Core ingest，硬断言入库记录、父子链、哈希、Registry 清空。CI：job 与测试统一读 `GATEWAY_E2E_API_KEY/BASE_URL/MODEL`；`if: github.event_name != 'pull_request' || !github.event.pull_request.head.repo.fork` 控制 secret job；缺 secret 时测试显式 fail（而非 skip）；`scripts/redact_ci_secrets.py` 从环境读 secret 列表做流式替换；job 末尾解析 pytest 输出断言 `passed > 0 && skipped_required == 0`。

### D10：三检查点交付（P1-7/§22）

- **Checkpoint 1**：Trace 身份 + Context 分槽 + Attempt Index/Registry（D1/D2/D4/D7 + test_trace_identity / test_context_lifecycle / test_open_attempt_cleanup）。
- **Checkpoint 2**：Streaming 终态 + Usage/Cost + Privacy + 事件生命周期 + 传播（D3/D5/D6/D8 + P1-3/P1-4/P1-6 + 对应测试）。
- **Checkpoint 3**：Real E2E + CI + Phase 2.1~2.5 回归 + 审查归档（D9/D10）。

## Risks / Trade-offs

- [删除 LLM=Router 回写后，依赖该语义的既有测试/面板数据变化] → 归类为语义 BREAKING，在 proposal 与 delta spec 中显式标注；Phase 2.x 回归中相关断言改为"LLM 记录逻辑 Usage"。
- [Context 分槽改动触及所有 close/reset 路径，回归面广] → 单一 `GatewayContextState` 入口 + 每个路径的对抗性测试（正常/异常/async/cross-context 四路）。
- [Streaming finalize funnel 若遗漏某个终止分支，会比现状更隐蔽] → 所有终止路径强制经 `_finalize_once`，测试覆盖 12 个终态用例（含 close/aclose 幂等）。
- [force_close 上报终态可能产生重复 Record] → 幂等守卫 + `test_router_finalize_after_attempt_end_does_not_duplicate_report`。
- [白名单默认拒绝可能漏掉未来新增合法字段] → 白名单集中在 contract 常量中，新增字段需显式登记（与契约评审绑定）。
- [CI 缺 secret 即失败会让未配置 secret 的 fork/新环境变红] → fork PR 整 job 跳过；受信分支失败是预期行为（P0-6 明确要求）。
- [哈希 channel ID 使跨环境排障需映射表] → 哈希稳定性测试保证同环境可追溯；原始值只允许出现在网关本地配置，不进入 Telemetry。

## Migration Plan

1. Checkpoint 1 合入：纯运行时内部语义修复，对外仅表现为 TraceID/Origin 正确化。
2. Checkpoint 2 合入：LLM Usage 语义切换（BREAKING，release note 说明 Retry Waste 改由 Core/UI 推导）；Streaming 状态一致性切换。
3. Checkpoint 3 合入：新 E2E + CI 门禁生效；旧 Phase 2.5 复用路径从 `gateway-real-e2e` job 移除。
4. 三层证据齐备（代码审查、CI 绿、Real E2E 0 skipped）后归档本 change；无需回滚策略——各 checkpoint 独立可 revert。

## Open Questions

- 客户端取消的最终状态映射（ERROR vs 独立 CANCELLED 语义）需产品确认；实现上按"Attempt/Router 一致"冻结，切换成本低。
- `x-llm-obs-*` 回传协议是否在本 Phase 落地，或推迟到 Phase 3.1+。
