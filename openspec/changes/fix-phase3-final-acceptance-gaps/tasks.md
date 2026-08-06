# Tasks: fix-phase3-final-acceptance-gaps

依据 proposal/design/spec 实施。冻结阻塞项（P0-1/P0-2）必须全绿才能视为可冻结。
所有并发测试用 `threading.Event` / `threading.Barrier` / `asyncio.Event` 制造确
定性竞争窗口，禁止 `sleep()` 猜时序。

## Checkpoint A — Streaming Terminal 原子状态机（P0-1，Blocker）

- [x] A.1 (Blocker) `streaming.py` `_TerminalFinalizer` 新增 `_terminal_lock = threading.Lock()`、`_terminal_state: Optional[str] = None`、`_terminal_time`，以及 `_claim_terminal(state) -> bool`（锁内 check-and-set，记录 `_terminal_time = time.time()`）
- [x] A.2 (Blocker) `finalize_success`/`finalize_error`/`finalize_cancelled` 改为先 `if not self._claim_terminal("success"/"error"/"cancelled"): return`，再执行 Span/Router/Event 修改；`finalized` 属性改为 `self._terminal_state is not None`
- [x] A.3 验证失败竞争路径不再调用 `_publish_and_close`、不再写 `attempt.set_error`、不再记 `stream.*` 事件
- [x] A.4 新增 `sdk/tests/gateway_observability/test_stream_terminal_atomicity.py`：
  - [x] A.4.1 `test_stream_exhaustion_racing_close_claims_one_terminal`
  - [x] A.4.2 `test_stream_error_racing_close_claims_one_terminal`
  - [x] A.4.3 `test_done_marker_racing_disconnect_claims_one_terminal`
  - [x] A.4.4 `test_async_cancel_racing_aclose_claims_one_terminal`
  - [x] A.4.5 `test_stream_terminal_records_one_stream_event`
  - [x] A.4.6 `test_stream_terminal_records_one_attempt_event`
  - [x] A.4.7 `test_stream_terminal_records_one_router_event`
  - [x] A.4.8 `test_stream_terminal_router_attempt_status_consistent`
  - [x] A.4.9 `test_stream_terminal_aggregates_exactly_once`
- [x] A.5 **Checkpoint A 审查**：A.1–A.4 完成、可编译、`pytest sdk/tests/gateway_observability/test_stream_terminal_atomicity.py -q` 全绿

## Checkpoint B — Terminal Event 互斥组（P1-2）

- [x] B.1 `recorder.py` 新增 `_TERMINAL_GROUPS = {"attempt": {...}, "response": {...}, "stream": {...}}`，`record()` 在按名去重之上加按组去重（同组已占用则拒绝）
- [x] B.2 将 `EVENT_STREAM_COMPLETED` / `EVENT_STREAM_CANCELLED` 纳入 `_TERMINAL_EVENTS` 与互斥组
- [x] B.3 `events.py` 新增 `EVENT_ATTEMPT_SELECTED = "gateway.attempt.selected"`，并补 `GatewayEventRecorder.attempt_selected(attempt_index, channel_id, provider, reason)`（channel_id 哈希）
- [x] B.4 新增 `sdk/tests/gateway_observability/test_terminal_event_exclusion.py`：
  - [x] B.4.1 `test_attempt_completed_then_failed_rejected`
  - [x] B.4.2 `test_attempt_failed_then_completed_rejected`
  - [x] B.4.3 `test_response_completed_then_failed_rejected`
  - [x] B.4.4 `test_stream_completed_then_cancelled_rejected`
- [x] B.5 **Checkpoint B 审查**：B.1–B.4 全绿

## Checkpoint C — Hedged / Parallel Winner 语义（P0-2，Blocker）

- [x] C.1 (Blocker) `router_span.py` `register_attempt_result` 改造：仍 sum `_success_count`/`_fail_count`/`_usage_aggregate`/`_cost_aggregate`/`_ttft_ms`，新增 `_results_by_index: dict[int, AttemptResult]` 存结果；**移除**对 `_final_channel_id`/`_final_http_status`/`_final_error` 的每次覆盖
- [x] C.2 (Blocker) 新增 `RouterSpan.select_winner(attempt_index, reason=None) -> bool`：校验已激活且有 `AttemptResult`；幂等；切换 Winner 记录 `gateway.attempt.selected`（reason 标识 re-selection）；在 `_aggregate_lock` 内设置 `_selected_attempt_index`/`_selected_result`/`_selection_reason` 并从 Winner 派生 `_final_*`
- [x] C.3 `RouterSpan.finalize()` / `_apply_aggregates()` Winner 解析：有 Winner → `final_*` 取自 Winner；无 Winner 且单一 Attempt → 自动 `select_winner(reason="auto_single_attempt")`；无 Winner 且多 Attempt → status=ERROR、`final_error_category=gateway_internal`、`final_error_type=MissingWinnerSelection`、记 `response.failed`
- [x] C.4 `errors.py` 新增 `ErrorType`/分类 `MissingWinnerSelection`（或等价 `gateway_internal` 子类型），并保证 `final_error_type` 属性可写
- [x] C.5 修正依赖旧"最后完成覆盖 final_*"语义的现有测试（改为显式 `select_winner` 或断言自动 Winner）—— 全量回归全绿，无需修正（auto-single-success / auto-single-attempt fail-safe 保留串行 Retry/Fallback 语义）
- [x] C.6 新增 `sdk/tests/gateway_observability/test_hedged_attempt_winner.py`：
  - [x] C.6.1 `test_hedged_success_then_loser_timeout_router_stays_ok`
  - [x] C.6.2 `test_hedged_loser_timeout_then_success_router_ok`
  - [x] C.6.3 `test_selected_attempt_defines_final_channel`
  - [x] C.6.4 `test_selected_attempt_defines_final_http_status`
  - [x] C.6.5 `test_selected_attempt_defines_final_error`
  - [x] C.6.6 `test_parallel_usage_includes_all_attempts`
  - [x] C.6.7 `test_parallel_cost_includes_losing_attempts`
  - [x] C.6.8 `test_select_winner_is_idempotent`
  - [x] C.6.9 `test_select_unknown_attempt_rejected`
  - [x] C.6.10 `test_multiple_attempts_without_winner_is_deterministic`
- [x] C.7 **Checkpoint C 审查**：C.1–C.6 全绿；串行 Retry 单成功 Attempt 场景行为与旧实现等价（自动 Winner）

## Checkpoint D — Streaming Duration 语义（P1-1）

- [x] D.1 `streaming.py` `_publish_and_close`（或 `_claim_terminal` 后）用 `_terminal_time - attempt._started_at` 覆盖 Attempt 的 `gateway.upstream_duration_ms`；保留 `upstream_connect_duration_ms`；非流式路径不动
- [x] D.2 新增 `sdk/tests/gateway_observability/test_stream_duration_semantics.py`：
  - [x] D.2.1 `test_stream_headers_duration_distinct_from_total_duration`
  - [x] D.2.2 `test_stream_total_duration_covers_consumption`
  - [x] D.2.3 `test_stream_cancel_total_duration_covers_partial_consumption`
  - [x] D.2.4 `test_non_streaming_duration_semantics_unchanged`
- [x] D.3 **Checkpoint D 审查**：D.1–D.2 全绿

## Checkpoint E — Phase 3 分阶段冻结文档（P0-3 / P0-4）

- [x] E.1 更新 Phase 3 Development Spec：拆分为 Phase 3.0（Contract & Runtime，本轮冻结）/ 3.1（One-API Production Integration，future change）/ 3.2（Gateway UI & Metrics，future change），明确 3.0 DoD 不含 One-API 真实接入与 UI/Metrics
- [x] E.2 更新 `CLAUDE.md` Current Status 与 Phase History：标注 3.0 FROZEN、3.1/3.2 PENDING；移除/修正"UI/Metrics 已完成"的表述
- [x] E.3 更新 Release Notes：记录本轮缺口修复 + 阶段拆分 + `select_winner`/`gateway.attempt.selected` 新增 API/事件
- [x] E.4 在 docs/ 标注 One-API 真实接入（Phase 3.1）与 Gateway UI/Metrics（Phase 3.2）为后续独立 change；不创建空白 spec 能力
- [x] E.5 **Checkpoint E 审查**：文档与实现范围一致；未在 DoD 宣称 3.1/3.2 已完成；未创建 `v3.0-complete` 标记

## Checkpoint F — 全量回归 + CI + 归档

- [x] F.1 全量回归：`.venv/bin/python -m pytest sdk/tests/ tests/ -q` 全绿（1006 passed, 1 skipped=live-upstream；含 Phase 2.1–2.5 regression 与全部已归档 closeout 测试）
- [ ] F.2 GitHub CI 全部成功；Real E2E `skipped == 0`、`passed > 0`；Secret 缺失受信任分支失败、Fork PR 跳过 Secret Job；日志脱敏（`scripts/redact_ci_secrets.py`）—— 需 push 后在 GitHub 验证
- [x] F.3 CI 新增/强化 `gateway-terminal-race-tests`、`gateway-hedged-attempt-tests` 挂在常驻 gateway test job（非 secret-gated）—— 新增 4 个测试文件已被 `gateway-runtime-tests`（整目录）覆盖，并补入 `gateway-streaming-tests` job
- [ ] F.4 **终审**：对照 design Risks 逐项核验；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备；对照 §13 验收矩阵逐项确认 —— 待 F.2 CI 绿
- [ ] F.5 归档本 change（`openspec archive fix-phase3-final-acceptance-gaps`），归档后同步主 spec Purpose；不修改已归档历史 —— 待 F.2/F.4
