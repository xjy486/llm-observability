# Tasks: fix-gateway-stream-publish-before-close

依据 proposal/design/spec 实施。冻结阻塞项必须全绿才能视为可冻结。

## Checkpoint A — streaming publish-before-close (Blocker)

- [x] A.1 (Blocker) `_TerminalFinalizer` 新增 `_publish_and_close(result)` 漏斗：`_aggregate_to_router(result)` → `_close_attempt()` → `_close_router()`
- [x] A.2 (Blocker) `finalize_success`/`finalize_error`/`finalize_cancelled` 三路径各自构造 `AttemptResult`（含已捕获 usage/error/cost/status）后统一调 `_publish_and_close(result)`；删除原 close→aggregate 顺序
- [x] A.3 **Checkpoint A 审查**：A.1–A.2 完成、可编译、现有 streaming 测试全绿

## Checkpoint B — publish-before-close 确定性测试（断言 Router Record）

- [x] B.1 新增 `test_stream_success_publishes_before_attempt_unregister`：patch `try_aggregate_result` 卡发布窗口 → 跨线程 finalize → 断言上报 Router Record 含 usage/cost/attempt_count
- [x] B.2 新增 `test_stream_error_publishes_before_router_report`：stream error 路径 → 断言 Record `final_error_category` = 业务错误、status=ERROR
- [x] B.3 新增 `test_stream_cancel_publishes_before_router_report`：cancel 路径 → 断言 Record `final_error_category` = client_cancelled、status=ERROR
- [x] B.4 新增 `test_stream_usage_cost_present_in_report_under_finalize_race`：发布窗口 + finalize 竞态 → Record 含 usage.total_tokens / cost.source
- [x] B.5 新增 `test_stream_reported_router_matches_final_memory_state`：Record 与最终内存一致（status/final_error_category/usage/cost/attempt_count）
- [x] B.6 **Checkpoint B 审查**：B.1–B.5 全绿

## Checkpoint C — spec 文字矛盾修正（P1）+ regression + archive

- [x] C.1 (P1) 主 spec lifecycle requirement 的 slot 清理文字已修正（Router dead→清整个；Attempt dead while Router alive→只清 Attempt slot）—— 已在本 change delta spec 中体现，归档后同步主 spec
- [x] C.2 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [ ] C.3 GitHub CI 全部成功（含 live-upstream 0 skipped 证据）
- [ ] C.4 **终审**：对照 design Risks 逐项核验；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [ ] C.5 归档本 change（`openspec archive fix-gateway-stream-publish-before-close`），不修改已归档历史
