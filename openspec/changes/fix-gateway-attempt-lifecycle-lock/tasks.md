# Tasks: fix-gateway-attempt-lifecycle-lock

依据 proposal/design/spec 实施。冻结阻塞项必须全绿才能视为可冻结。

## Checkpoint A — Attempt lifecycle lock (Blocker)

- [x] A.1 (Blocker) `AttemptSpan` 新增 `self._lifecycle_lock = threading.RLock()`；提取 `_activate_context_and_started_event()`：锁内 re-check `_closed/_no_op` → `set_attempt` → post-install re-check `_closed`（若被 force_close 则 `_cleanup_context_if_owned` 并返回 False）→ `attempt.started`；`start()` 改调此方法
- [x] A.2 (Blocker) `force_close()` 在 `_lifecycle_lock` 内做 `_closed` 状态转换（RLock 允许 force_close→close 重入）
- [x] A.3 (Blocker) `close()` 在 `_lifecycle_lock` 内做 `_closed` 状态转换与早返回清理
- [x] A.4 **Checkpoint A 审查**：A.1–A.3 完成、可编译

## Checkpoint B — 确定性窗口测试（patch set_attempt 在方法内阻塞）

- [x] B.1 新增 `test_finalize_after_closed_check_before_set_attempt_no_leak`：patch `GatewayContext.set_attempt` 进入后阻塞 → finalize → 释放 → 断言 `active_attempt is None`、attempt._closed、无迟到 started 事件
- [x] B.2 新增 `test_no_started_event_when_force_close_occurs_inside_set_attempt_window`：同窗口断言 ended span 无 `gateway.attempt.started`
- [x] B.3 新增 `test_owner_never_calls_close_after_race_context_still_empty`：窗口竞态后**不**调 `attempt.close()`，断言 `active_attempt is None`（不依赖业务方 close 兜底）
- [x] B.4 **Checkpoint B 审查**：B.1–B.3 全绿

## Checkpoint C — 并行结果聚合锁（P1）

- [x] C.1 (P1) `RouterSpan` 新增 `self._aggregate_lock = threading.RLock()`；`register_attempt_result` 整个状态更新入锁；`set_usage_aggregate`/`set_cost_aggregate` 入锁；`_apply_aggregates` 读取时入锁取一致快照
- [x] C.2 (P1) 新增测试：`test_parallel_attempt_results_count_exact` / `test_parallel_attempt_usage_sum_exact` / `test_parallel_attempt_cost_sum_exact` / `test_parallel_success_failure_counts_exact`
- [x] C.3 **Checkpoint C 审查**：C.1–C.2 全绿

## Checkpoint D — regression + archive

- [x] D.1 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [ ] D.2 GitHub CI 全部成功（含 live-upstream 0 skipped 证据）
- [ ] D.3 **终审**：对照 design Risks 逐项核验（锁嵌套无死锁）；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [ ] D.4 归档本 change（`openspec archive fix-gateway-attempt-lifecycle-lock`），不修改已归档历史
