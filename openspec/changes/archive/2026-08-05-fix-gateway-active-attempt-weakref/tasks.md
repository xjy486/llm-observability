# Tasks: fix-gateway-active-attempt-weakref

依据 proposal/design/spec 实施。冻结阻塞项必须全绿才能视为可冻结。

## Checkpoint A — weakref active-attempt + lazy invalidation (Blocker)

- [x] A.1 (Blocker) `GatewayContextState.active_attempt` 改为弱引用 holder（`ActiveAttemptRef(weakref.ref(attempt))`）；`set_attempt` 存弱引用；Router slot 保持不变
- [x] A.2 (Blocker) `GatewayContext.get()` 懒解引用：referent 死亡或 `_closed` → 清当前线程 attempt slot（per-thread `ContextVar.set`，无需 foreign token）→ `active_attempt` 读 None
- [x] A.3 (Blocker) `Runtime.active_attempt()` 经 `get()` 读取，永不返回已结束 Attempt；`clear_attempt`/`clear_attempt_only` 适配弱引用
- [x] A.4 **Checkpoint A 审查**：A.1–A.3 完成、可编译

## Checkpoint B — 跨线程全激活 + 线程池复用测试

- [x] B.1 新增 `test_cross_thread_force_close_after_full_activation_clears_owner_context`：线程 A 完整 start()（不 close）→ 线程 B finalize → 断言线程 A 读 `active_attempt is None`
- [x] B.2 新增 `test_owner_never_closes_after_full_activation_context_not_stale`：同上但断言 `Runtime.active_attempt()` 不返回已结束 Attempt
- [x] B.3 新增 `test_thread_pool_worker_reused_after_force_close_has_no_active_attempt`：同一 worker 跑 Task1（被跨线程 force_close）→ Task2 读 Context → `active_attempt is None`
- [x] B.4 **Checkpoint B 审查**：B.1–B.3 全绿

## Checkpoint C — try_aggregate_result 单一聚合漏斗（P1）

- [x] C.1 (P1) `AttemptSpan.try_aggregate_result(result) -> bool`：`_lifecycle_lock` 内原子 check-and-set `_aggregated_to_router`，仅首调用返回 True 并调 `router.register_attempt_result`（在 Attempt 锁外，经 Router `_aggregate_lock`）
- [x] C.2 (P1) 三处聚合点改经 `try_aggregate_result`：`runtime.finalize_attempt`、streaming `_aggregate_to_router`、`attempt._aggregate_force_close_result`（后者已在 `_lifecycle_lock` 内，RLock 重入安全）
- [x] C.3 (P1) 新增测试：`test_finish_attempt_racing_force_close_aggregates_once` / `test_stream_finalize_racing_router_finalize_aggregates_once` / `test_same_attempt_success_failure_race_count_equals_one`
- [x] C.4 **Checkpoint C 审查**：C.1–C.3 全绿

## Checkpoint D — regression + archive

- [x] D.1 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [x] D.2 GitHub CI 全部成功（含 live-upstream 0 skipped 证据）
- [x] D.3 **终审**：对照 design Risks 逐项核验；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [x] D.4 归档本 change（`openspec archive fix-gateway-active-attempt-weakref`），不修改已归档历史
