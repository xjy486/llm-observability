# Tasks: fix-gateway-native-observability-closeout

依据 `docs/llm-observability-phase3-rework-bug-fix-requirements.md` §17 实施顺序与 §22 三检查点要求。每个任务附对抗性测试；测试名与文档 §2-§15 一一对应。

## Checkpoint 1 — Trace + Context + Attempt Lifecycle

- [x] 1.1 (P0-1) Router Parent Resolver 返回 `ResolvedGatewayParent{trace_id, parent_span_id, origin, upstream_trace_present}`；无 SDK/无 traceparent 时生成 32 位十六进制、非全 0、连续唯一的 TraceID；Attempt 继承 Router TraceID
- [x] 1.2 (P0-1) 新增 `sdk/tests/gateway_observability/test_trace_identity.py`：`test_no_sdk_router_generates_valid_trace_id` / `test_no_sdk_attempt_inherits_router_trace_id` / `test_no_sdk_requests_generate_distinct_trace_ids` / `test_router_never_reports_null_trace_id` / `test_router_never_reports_all_zero_trace_id`
- [x] 1.3 (P0-2) `gateway.trace_origin` 冻结为 sdk/remote/gateway 三值并由 `ResolvedGatewayParent.origin` 直接映射；`upstream_trace_present` 与 origin 严格一致；禁止从"是否存在 parent 对象"间接推断
- [x] 1.4 (P0-2) 测试：`test_sdk_context_sets_trace_origin_sdk` / `test_remote_traceparent_sets_trace_origin_remote` / `test_remote_traceparent_sets_upstream_trace_present_true` / `test_local_root_sets_trace_origin_gateway` / `test_trace_metadata_consistent_with_parent_ids`
- [x] 1.5 (P0-3) Gateway Context 拆为 Router/Attempt 双槽（`GatewayContextState`）；Attempt close/error/async/cross-context reset 只清 Attempt 槽；`clear_gateway_context()` 仅 Router 终态调用
- [x] 1.6 (P0-3) 新增 `test_context_lifecycle.py`：`test_attempt_close_preserves_active_router` / `test_attempt_error_preserves_active_router` / `test_retry_second_attempt_uses_same_router` / `test_fallback_second_attempt_uses_same_router` / `test_attempt_close_clears_only_attempt_slot` / `test_router_close_clears_router_and_attempt_slots` / `test_async_attempt_close_preserves_router` / `test_cross_context_attempt_reset_preserves_router`
- [x] 1.7 (P1-1) `router.allocate_attempt_index()` 默认递增且并发安全；显式合法值采用、重复重映射+warning、非法值回退自动分配；`attempt_count` 等于实际 Attempt 数
- [x] 1.8 (P1-1) 测试：`test_default_attempt_index_increments` / `test_attempt_count_matches_actual_attempts` / `test_duplicate_explicit_attempt_index_handled` / `test_invalid_attempt_index_falls_back` / `test_parallel_attempt_index_is_thread_safe`
- [x] 1.9 (P0-5) Router 维护 `_open_attempts`（start 注册 / close 注销，注销键为 Attempt 自身标识）；`finalize()` 对遗留 Attempt 调用幂等 fail-open 的 `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`，不覆盖已有业务错误、清 Registry+Context、上报终态
- [x] 1.10 (P0-5) 新增 `test_open_attempt_cleanup.py`：`test_router_finalize_force_closes_open_attempt` / `test_router_finalize_multiple_open_attempts` / `test_exception_between_attempt_start_and_close_no_leak` / `test_force_close_is_idempotent` / `test_router_finalize_after_attempt_end_does_not_duplicate_report` / `test_open_attempt_registry_empty_after_router_finalize`
- [x] 1.11 **Checkpoint 1 审查**：1.1–1.10 全部测试通过、代码审查通过后进入 Checkpoint 2

## Checkpoint 2 — Streaming + Usage/Cost + Privacy

- [x] 2.1 (P0-4) 拆分 `finish_non_streaming_attempt(...)` / `finalize_streaming_attempt(...)`；禁止响应头/Wrapper 创建阶段聚合成功；所有终态经单一幂等 finalize funnel：构造最终 `AttemptResult` → 聚合 Router（恰好一次）→ close Attempt → close Router
- [x] 2.2 (P0-4) Streaming 终态一致性：Success（Attempt/Router 均 OK）、Error（Attempt error_category → Router final_error_category）、Cancel（均 client_cancelled，状态映射一致）；TTFT 从真实上游请求开始计算，忽略 keepalive/空串/metadata-only/usage-only/`[DONE]`；终态 chunk Usage 聚合到 Router，取消/失败时按 Provider 能力记录部分 Usage
- [x] 2.3 (P0-4) 新增 `test_stream_terminal_state.py`：`test_stream_success_aggregates_attempt_once` / `test_stream_cancel_router_and_attempt_both_error` / `test_stream_error_router_and_attempt_both_error` / `test_stream_timeout_router_final_error_timeout` / `test_stream_does_not_aggregate_success_before_terminal_state` / `test_stream_attempt_result_registered_once` / `test_stream_usage_aggregated_at_terminal_chunk` / `test_stream_ttft_ignores_keepalive` / `test_stream_ttft_ignores_done_marker` / `test_stream_close_is_idempotent` / `test_async_stream_cancelled_error_finalizes_once` / `test_async_stream_aclose_finalizes_once`
- [x] 2.4 (P0-7) 删除"LLM Usage 必须等于 Router Aggregate"的 ContextVar 回写语义（`instrumentation/openai.py`、LangChain `llm_span.py`）；Attempt 持单次 Usage/Cost、Router 汇总含失败重试、LLM 持逻辑响应 Usage；Retry Waste 由 Core/UI 推导
- [x] 2.5 (P0-7) 新增 `test_usage_ownership.py`：`test_router_usage_is_sum_of_all_attempts` / `test_llm_usage_remains_logical_response_usage` / `test_failed_retry_usage_counted_in_router` / `test_retry_waste_can_be_derived` / `test_cross_process_trace_does_not_require_shared_contextvar`
- [x] 2.6 (P1-2) Cost 使用 `attempt.resolved_model`；价格表单位冻结 USD/1M tokens（`input_usd_per_1m_tokens` / `output_usd_per_1m_tokens`）；无价格 `cost.source=unpriced`；cache 显式 cost 保留、未传按 resolved model 计算
- [x] 2.7 (P1-2) 测试：`test_cost_uses_resolved_model` / `test_cost_unknown_model_is_unpriced` / `test_cache_explicit_cost_is_preserved` / `test_retry_cost_sums_all_attempts` / `test_failed_attempt_cost_included` / `test_pricing_unit_is_per_1m_tokens`
- [x] 2.8 (P0-8) `fallback.selected` 冻结 `from_channel_id`/`to_channel_id`/`reason`；Route/Attempt/Fallback Event 及日志/Metrics 中所有 channel ID 统一经 `PrivacyGuard.hash_channel_id()`；任何 Telemetry 不出现原始 Channel ID
- [x] 2.9 (P0-8) 新增 `test_channel_privacy.py`：`test_route_event_channel_id_is_hashed` / `test_attempt_event_channel_id_is_hashed` / `test_fallback_event_contains_from_and_to` / `test_fallback_event_from_and_to_are_hashed` / `test_raw_channel_id_absent_from_all_span_events_logs` / `test_same_channel_id_hash_is_stable` / `test_different_channel_ids_hash_differ`
- [x] 2.10 (P1-3) `route_selected` / `attempt_started` / `attempt_completed` / `attempt_failed` / `response_completed` / `response_failed` 真正接入 Runtime 生命周期；每个终态事件最多一次、span end 前写入、经 PrivacyGuard；同一 Attempt 不得同时有 completed 与 failed
- [x] 2.11 (P1-3) 测试：`test_attempt_start_event_exactly_once` / `test_attempt_completed_event_exactly_once` / `test_attempt_failed_event_exactly_once` / `test_router_response_completed_exactly_once` / `test_router_response_failed_exactly_once` / `test_no_success_and_failed_events_on_same_attempt`
- [x] 2.12 (P1-4) Router 完整写入 `user_id`/`session_id`/`message_id`/`app_name`/business 字段（单一命名，与现有 Span Record 一致）；优先级：显式值 > Remote Header/Baggage > None；复用 Phase 2.5 Association Resolver；值经脱敏
- [x] 2.13 (P1-4) 测试：`test_router_all_association_fields` / `test_attempt_does_not_duplicate_sensitive_association` / `test_remote_association_propagates_to_router` / `test_local_gateway_association_overrides_remote` / `test_association_values_are_sanitized`
- [x] 2.14 (P1-5) 统一入口 `set_gateway_attribute(span, key, value, privacy_guard)`：字段白名单（默认拒绝未知 key）、值脱敏、长度限制（512B/256B/256B/128B）、类型规范化、Size Guard、fail-open；Router/Attempt 禁止直接写未保护的外部字符串
- [x] 2.15 (P1-5) 测试：`test_router_external_values_sanitized` / `test_attempt_external_values_sanitized` / `test_request_id_size_limited` / `test_route_query_removed` / `test_error_message_secret_redacted` / `test_span_attributes_default_deny_unknown_keys`
- [x] 2.16 (P1-6) 提供 `inject_downstream_trace_headers(router, attempt)`：下游 traceparent 的 trace_id=Router.trace_id、parent=Attempt.span_id、flags 继承 sampled（0→`00`，1→`01`）
- [x] 2.17 (P1-6) 新增 `test_downstream_propagation.py`：`test_attempt_downstream_traceparent_parent_is_attempt` / `test_sampled_zero_downstream_trace_flags_00` / `test_sampled_one_downstream_trace_flags_01` / `test_remote_trace_id_preserved_downstream` / `test_local_root_trace_id_propagated_downstream`
- [x] 2.18 **Checkpoint 2 审查**：2.1–2.17 全部测试通过、代码审查通过后进入 Checkpoint 3

## Checkpoint 3 — Real E2E + CI + Regression

- [x] 3.1 (P0-6) 新增 `sdk/tests/gateway_observability/test_real_gateway_e2e.py`：Client → Middleware/Adapter → Runtime → Router → Attempt → Mock/真实 Upstream → Reporter → Mock Core Ingest 全链路；覆盖成功/Retry/Fallback/Streaming Success/Streaming Cancel/无 SDK Trace/sampled=0/隐私
- [x] 3.2 (P0-6) E2E 硬断言：Router/Attempt 实际入 Core、TraceID 合法、Attempt.parent=Router、Router.parent=SDK LLM 或 Remote Parent、Retry 产生多个唯一 Attempt、Fallback from/to 正确且已哈希、Streaming 终态一致、Registry/Context 最终为空
- [x] 3.3 (P0-6) CI 统一变量 `GATEWAY_E2E_API_KEY`/`GATEWAY_E2E_BASE_URL`/`GATEWAY_E2E_MODEL`（job 与测试同名）；可信分支缺 Secret → job 失败；Fork PR → 整个 secret job 不执行；`gateway-real-e2e` job 改为运行新 E2E 文件，不再复用 Phase 2.5 测试
- [x] 3.4 (P0-6) 新增 `scripts/redact_ci_secrets.py`（从环境变量读 secret 列表做流式替换），替换单引号 sed 脱敏
- [x] 3.5 (P0-6) CI 新增门禁：运行测试数 > 0 且必需 E2E skipped = 0
- [x] 3.6 Phase 2.1～2.5 全量回归通过（SDK + proxy/core 156+ 测试）
- [ ] 3.7 GitHub CI 全部成功（含 gateway-real-e2e 0 skipped 证据）
- [ ] 3.8 **终审**：对照文档 §20 Definition of Done 25 条逐项核验；三层证据（代码审查 / CI 绿 / Real E2E 0 skipped）齐备
- [ ] 3.9 归档本 change（`openspec archive fix-gateway-native-observability-closeout`），不得修改已归档历史
