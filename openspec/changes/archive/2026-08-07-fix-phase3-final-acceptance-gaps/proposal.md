# Proposal: fix-phase3-final-acceptance-gaps

## Why

Phase 3 — Gateway Native Observability 的 Contract 与 Runtime 主体已完成、CI 与
Gateway E2E 已通过，但完整 Phase 3 DoD 仍有运行时并发终态、Hedged Attempt 最终
胜者语义、Streaming Duration 字段语义、Terminal Event 互斥等验收缺口。当前
`_TerminalFinalizer` 用普通布尔 `self._finalized` 做终态声明，read-check-write
非原子，并发竞争下同一 Stream 可能同时落入 `finalize_success` 与
`finalize_cancelled`，破坏 Router/Attempt 终态一致性；Router 的
`register_attempt_result` 在每次 Attempt 聚合时覆盖 `final_channel_id` /
`final_http_status` / `final_error`，等价于"最后完成的 Attempt 决定 Router 终
态"，在 Hedged/Parallel 场景下会用 loser 的 timeout 覆盖已返回用户的成功结果；
Recorder 只按事件名去重，`attempt.completed`+`attempt.failed`、
`stream.completed`+`stream.cancelled` 仍可并存；Streaming 的
`gateway.upstream_duration_ms` 在响应头时刻写入后不再更新，与"完整上游流生命周
期"语义不一致。本轮修复这些缺口并把 One-API 生产接入、Gateway UI/Metrics 正式
拆分为 Phase 3.1 / 3.2，使 Phase 3.0 可冻结。

依据：`docs/llm-observability-phase3-final-acceptance-gap-fix-requirements.md`。

## What Changes

- **P0-1 Streaming Terminal 原子状态机（Blocker）。** `_TerminalFinalizer` 新增
  `threading.Lock` 保护的 `_claim_terminal(state) -> bool`；`finalize_success` /
  `finalize_error` / `finalize_cancelled` 改为先 claim terminal、claim 失败则直接
  no-op 返回，不再修改 Span / Router 聚合 / Terminal Event。首版语义为
  **first terminal claim wins**（不通过时间戳猜测、不隐式覆盖）。只有赢得 claim
  的路径可写 Attempt Error/Status、聚合 Router、记录 Terminal Event、关闭
  Attempt/Router。

- **P0-2 Hedged / Parallel Attempt Winner 语义（Blocker）。** 区分"全部 Attempt
  聚合"（Usage/Cost/success_count/fail_count/attempt_count/retry_count/fallback_count
  仍聚合所有 Attempt）与"业务最终 Winner"（Router final status / final channel /
  final HTTP status / final error / finish reason）。Router 新增
  `select_winner(attempt_index, reason=None) -> bool`：只允许选择已激活且已有
  `AttemptResult` 的 Attempt，默认只允许设置一次，重复选择同一 Attempt 幂等，切
  换 Winner 记录事件。Router 的 `final_*` 字段改由 Winner 决定，而非每次聚合覆
  盖。Fail-safe：无 Winner 且仅一个 Attempt → 自动选择该 Attempt；无 Winner 且
  多 Attempt → Router final status = ERROR、
  `gateway.final_error_category = gateway_internal`、
  `gateway.final_error_type = MissingWinnerSelection`。新增事件
  `gateway.attempt.selected`（属性 `attempt_index` / `channel_id` / `provider` /
  `reason`，Channel ID 哈希）。

- **P1-1 Streaming Duration 字段语义。** Streaming 终态时用
  `terminal_time - attempt_start_time` 覆盖为完整上游流生命周期耗时，冻结
  `gateway.upstream_duration_ms = 完整上游流生命周期`（不再保留响应头时刻的
  duration 作为总耗时）；保留 `gateway.upstream_connect_duration_ms`。非流式语
  义不变。

- **P1-2 Terminal Event 互斥组。** `GatewayEventRecorder` 新增互斥组
  (`attempt`、`response`、`stream`)，同一组记录一个事件后其余事件全部拒绝（不
  仅仅按事件名去重）。`stream.completed` / `stream.cancelled` 纳入 `stream` 互
  斥组并加入终态去重集合。

- **Phase 3 分阶段冻结。** 正式拆分为 Phase 3.0（Gateway Contract & Runtime，
  本轮冻结）、Phase 3.1（One-API Production Integration，未来 change）、Phase 3.2
  （Gateway UI & Metrics，未来 change）。本轮更新 Development Spec / OpenSpec
  / CLAUDE.md / DoD / Release Notes，使文档与"Phase 3.0 仅冻结 Runtime/Contract"
  一致；不得保留"UI/Metrics 已完成"的文档表述而代码未实现。

本轮**不**进入：One-API 真实生命周期接入（Phase 3.1）、Gateway 专用 UI 与
Metrics（Phase 3.2）、Embedding/Rerank、LangGraph、MCP、自动路由/降级。

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-contract`:
  - Streaming lifecycle 要求新增"同一 Stream 恰好一个 Terminal State、first
    terminal claim wins"的契约保证；
  - Gateway events 要求新增 `gateway.attempt.selected` 事件（固定事件名 + 有限
    属性 + Channel ID 哈希）；
  - Usage/cost ownership 与 Retry/fallback/cache/rate-limit 要求明确"Router
    final status/channel/http_status/error/finish_reason 由显式 Winner 决定，
    Usage/Cost 仍聚合全部 Attempt"以及无 Winner 的 Fail-safe 语义。

- `gateway-observability-runtime`:
  - Streaming wrapper lifecycle 要求新增原子终态状态机（`_claim_terminal`）与
    Streaming Duration 语义（终态覆盖为完整上游流生命周期）；
  - GatewayEventRecorder 要求新增 Terminal Event 互斥组；
  - UsageNormalizer/CostCalculator 要求（`register_attempt_result`）新增
    `select_winner` API、Winner 驱动 `final_*` 字段、聚合仍 sum 所有 Attempt、
    无 Winner Fail-safe。

## Impact

- **Code:**
  `sdk/python/llm_observability/gateway_observability/streaming.py`
  (`_TerminalFinalizer._claim_terminal`、三 finalize 路径、`_publish_and_close`、
  duration 覆盖),
  `router_span.py` (`select_winner`、`_selected_attempt_index`/`_selected_result`/
  `_selection_reason`、`register_attempt_result` 不再覆盖 `final_*`、finalize 时
  Winner 解析与 Fail-safe),
  `recorder.py` (互斥组 + `stream.*` 纳入终态去重),
  `events.py` (`EVENT_ATTEMPT_SELECTED`).
- **Tests:** 新增
  `sdk/tests/gateway_observability/test_stream_terminal_atomicity.py`、
  `test_terminal_event_exclusion.py`、
  `test_hedged_attempt_winner.py`、
  `test_stream_duration_semantics.py`（均用 `threading.Event`/`Barrier`/
  `asyncio.Event` 制造确定性竞争窗口，禁止 `sleep()` 猜时序）。
- **Docs:** Phase 3 Development Spec、OpenSpec 主 spec（归档后同步）、CLAUDE.md
  Phase Status、Release Notes —— 反映 3.0/3.1/3.2 拆分。
- **CI:** 新增/强化 `gateway-terminal-race-tests`、`gateway-hedged-attempt-tests`
  挂在常驻 gateway test job；继续保证 Real E2E 0 skipped、Secret 缺失受信任分支
  失败、Fork PR 跳过 Secret Job、日志脱敏。
- **Regression:** Phase 2.1–2.5 与全部已归档 closeout 测试保持全绿；无新
  SpanKind；telemetry 保持 fail-open；不侵入 One-API 路由语义。
- **Non-breaking:** `select_winner` 为新增 API；`final_*` 由 Winner 决定是行为收
  紧（原先隐式"最后完成"），但单 Attempt 串行场景自动 Winner，行为等价。
