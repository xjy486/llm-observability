# Tasks: fix-gateway-attempt-activation-race

依据 proposal/design/spec 实施。冻结阻塞项必须全绿才能视为可冻结。

## Checkpoint A — race fixes (code)

- [x] A.1 (race 1) `AttemptSpan.start()` 在 `register_open_attempt` 返回 True 后、`set_attempt` 前 re-check `self._closed`；若已 force-closed 则不设 ContextVar、不记 `attempt.started`（attempt 已被 force_close 结束+上报）
- [x] A.2 (race 1) `AttemptSpan.close()` 在 `_closed=True` 早返回路径上调用新增 `_cleanup_context_if_owned()`：若 `_ctx_token` 存在且当前 `active_attempt is self` 则清理（fail-open）
- [x] A.3 (race 2) `RouterSpan.attempt()` 在 `_open_attempts_lock` 内 check `_closed` + `allocate_attempt_index`（嵌套 `_index_lock`，顺序 open_attempts→index 一致）；closed 时返回 no-op 且不分配 index
- [x] A.4 (race 3) `AttemptSpan.start()` 拒绝路径上 set `self._span = None`（不残留 started-but-unended Span）
- [x] A.5 **Checkpoint A 审查**：A.1–A.4 完成、单测可编译

## Checkpoint B — deterministic barrier tests

- [x] B.1 新增 `test_finalize_after_register_before_context_set_no_context_leak`：patch `register_open_attempt` 在原方法后用 Event 阻塞 → finalize → 释放 → 断言 `active_attempt is None` 且 attempt._closed
- [x] B.2 新增 `test_no_started_event_after_attempt_force_closed`：同窗口断言 ended span 上无 `gateway.attempt.started` 事件
- [x] B.3 新增 `test_force_close_in_other_thread_then_owner_close_clears_context`：白盒构造 closed attempt 持有 token → owner close() → 断言 ContextVar 已清
- [x] B.4 新增 `test_rejected_attempt_does_not_increment_attempt_count`：finalize 后 start_attempt → no-op → `_attempt_count` 不变
- [x] B.5 新增 `test_attempt_allocation_racing_finalize_not_in_router_count`：patch `allocate_attempt_index` 在锁内阻塞 → finalize → 释放 → 断言 `attempt_count` 不变
- [x] B.6 新增 `test_worker_thread_context_empty_after_finalize_race`：worker 线程内跑窗口竞态 → 线程退出后断言其 `active_attempt is None`
- [x] B.7 **Checkpoint B 审查**：B.1–B.6 全绿

## Checkpoint C — regression + archive

- [x] C.1 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [ ] C.2 GitHub CI 全部成功（含 live-upstream 0 skipped 证据）
- [ ] C.3 **终审**：对照 design Risks 逐项核验；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [ ] C.4 归档本 change（`openspec archive fix-gateway-attempt-activation-race`），不修改已归档历史
