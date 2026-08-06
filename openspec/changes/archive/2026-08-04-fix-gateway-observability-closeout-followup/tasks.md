# Tasks: fix-gateway-observability-closeout-followup

依据 proposal/design/spec 实施。每个任务附对抗性测试；测试名与契约 scenario 一一对应。冻结阻塞项（Blocker 1/2）必须全绿才能视为可冻结。

## Checkpoint A — Blocker 1 + Blocker 2 (freeze-blocking)

- [x] A.1 (Blocker 1) `AttemptSpan.force_close()` 构造终态 `AttemptResult(success=False, error=<gateway_internal GatewayError>, usage=attempt.usage, cost=attempt.cost)` 并经 `_aggregated_to_router` 守卫幂等聚合到 Router；不覆盖已有业务错误
- [x] A.2 (Blocker 1) Router finalize 后：`fail_count` 递增、`final_error_category=gateway_internal`、Router status=ERROR、恰好一次 `gateway.response.failed`、Registry 空、无重复 report
- [x] A.3 (Blocker 1) 新增 `test_force_close_aggregation.py`：`test_force_closed_attempt_makes_router_error` / `test_force_closed_attempt_sets_router_final_error` / `test_force_closed_attempt_records_response_failed` / `test_multiple_force_closed_attempts_increment_fail_count` / `test_force_closed_attempt_with_business_error_preserves_it` / `test_force_closed_attempt_does_not_duplicate_report`
- [x] A.4 (Blocker 2) Router/Attempt 外部字符串属性统一经 `set_gateway_attribute(span, key, value, self._privacy)`：request_id/route/provider/resolved_model/requested_model/route_reason/policy_name/channel_type/version/name/protocol/upstream_request_id/error_type/error_message/finish_reason；内部计数/布尔/已哈希 channel_id/数值指标保持直接写
- [x] A.5 (Blocker 2) 新增 `test_runtime_privacy_integration.py`（经真实 GatewayRuntime/RouterSpan/AttemptSpan 注入恶意值）：`test_runtime_route_query_removed` / `test_runtime_request_id_size_limited` / `test_runtime_route_reason_secret_redacted` / `test_runtime_upstream_request_id_sanitized` / `test_runtime_attempt_error_message_redacted` / `test_runtime_unknown_gateway_attribute_rejected`
- [x] A.6 **Checkpoint A 审查**：A.1–A.5 全绿、代码审查通过后进入 Checkpoint B

## Checkpoint B — P1-1 / P1-2 / P1-4 (hardening)

- [x] B.1 (P1-1) `_TerminalFinalizer` 接收 `cost_calculator` + `resolved_model`；`finalize_success/error/cancelled` 在捕获 usage 后计算 `cost=cost_calculator.calculate(usage, model=resolved_model)`（fail-open），`attempt.set_cost(cost)`，`AttemptResult.cost=cost`
- [x] B.2 (P1-1) `GatewayStream`/`AsyncGatewayStream`/`wrap_stream`/`wrap_async_stream`/`GatewayRuntimeHandle.finalize_streaming_attempt` 透传 `cost_calculator`（取自 runtime）与 `resolved_model`（取自 attempt）
- [x] B.3 (P1-1) 在 `test_stream_terminal_state.py` 新增：`test_streaming_cost_computed_from_terminal_usage`（有价） / `test_streaming_cost_unpriced_when_no_pricing`（无价） / `test_streaming_cancel_partial_usage_cost_fail_open`
- [x] B.4 (P1-2) `_TerminalFinalizer._classify`：`classify_error` 返回 `UNKNOWN` 时映射为 `STREAM_INTERRUPTED`（仅 streaming funnel，不改全局）
- [x] B.5 (P1-2) 在 `test_stream_terminal_state.py` 新增：`test_stream_generic_error_classified_stream_interrupted`（ValueError/解析错误 → stream_interrupted，非 unknown）
- [x] B.6 (P1-4) Router 新增 `_open_attempts_lock = threading.RLock()`，覆盖 register/unregister/`open_attempts` 快照/`open_attempt_count`/`_force_close_open_attempts` 的快照+clear
- [x] B.7 (P1-4) 在 `test_open_attempt_cleanup.py` 新增并发测试：`test_concurrent_attempt_register_unregister_safe` / `test_finalize_snapshot_stable_under_concurrent_close`（threading 并发启动/关闭，无遗漏/无双计）
- [x] B.8 **Checkpoint B 审查**：B.1–B.7 全绿后进入 Checkpoint C

## Checkpoint C — P1-3 (real HTTP E2E) + regression + archive

- [x] C.1 (P1-3) 新增 `sdk/tests/gateway_observability/gateway_http_harness.py`：`MockCoreServer`（aiohttp，`POST /api/v1/ingest` 真实 HTTP 存储）+ `GatewayHarness`（aiohttp，`POST /v1/chat/completions` + 流式，真实跑 GatewayRuntime → adapter → handle_request → start_attempt → mock upstream → finalize → 返回）
- [x] C.2 (P1-3) E2E 走真实 HTTP：SDK `Reporter(endpoint=<core url>).start_sync()` 真实 HTTP 上报；httpx/aiohttp client → GatewayHarness → mock upstream → Reporter HTTP → MockCore HTTP
- [x] C.3 (P1-3) 新增 `test_gateway_http_e2e.py`：success / retry / fallback / streaming success / streaming cancel / no-SDK trace / sampled=0 / privacy，硬断言经真实 HTTP 入 Core（TraceID 合法、Attempt.parent=Router、fallback from/to 已哈希、Streaming 终态一致、Registry/Context 空）
- [x] C.4 (P1-3) 既有 live-upstream 测试重命名为 `test_live_upstream_runtime_e2e`（明确其验证 Runtime 主链路 + 真实上游，非服务器级），保持 secret-gated；更新 `test_real_gateway_e2e.py` 文件头注释
- [x] C.5 (P1-3) `gateway-runtime-tests` CI job 纳入新的确定性 HTTP E2E（mock upstream，不依赖 secret）；`gateway-real-e2e` 仍跑 live-upstream（secret-gated）
- [x] C.6 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [x] C.7 GitHub CI 全部成功（含新 HTTP E2E、live-upstream 0 skipped 证据）
- [x] C.8 **终审**：对照 design Risks 逐项核验；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [x] C.9 归档本 change（`openspec archive fix-gateway-observability-closeout-followup`），不修改已归档历史
