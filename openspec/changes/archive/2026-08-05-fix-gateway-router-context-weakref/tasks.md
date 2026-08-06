# Tasks: fix-gateway-router-context-weakref

依据 proposal/design/spec 实施。冻结阻塞项（P0-1/P0-2）必须全绿才能视为可冻结。

## Checkpoint A — Router slot weak + lazy invalidation (P0-1)

- [x] A.1 (P0-1) `GatewayContextState` 的 Router slot 改为弱引用（`_router_ref`）；Attempt slot 重命名 `_active_attempt_ref`；`router`/`active_attempt` 改为 `@property` 解引用（公开 API 语义不变）
- [x] A.2 (P0-1) `GatewayContext.get()` 同时懒失效两个 slot：Router 死亡/`_closed` 或 Attempt 死亡/`_closed` → 清当前线程整个 state（per-thread `ContextVar.set`）
- [x] A.3 (P0-1) `Runtime.active_router()` 经 `get().router` 读取，永不返回已结束 Router；`enter_router` 存弱引用
- [x] A.4 **Checkpoint A 审查**：A.1–A.3 完成、可编译

## Checkpoint B — 跨线程 Router finalize + 线程池复用测试

- [x] B.1 新增 `test_cross_thread_router_finalize_hides_closed_router`：线程 A 装 Router → 线程 B `handle.finalize()` → 断言线程 A `active_router is None`
- [x] B.2 新增 `test_thread_pool_worker_reuse_has_no_active_router`：单 worker 池，Task1 被**跨线程 `handle.finalize()`** → Task2 同 worker 读 → `active_router is None` 且 `active_attempt is None`
- [x] B.3 新增 `test_runtime_active_router_never_returns_closed_router`：白盒 `_closed=True` → `active_router()` 返回 None
- [x] B.4 新增 `test_cross_thread_finalize_releases_router_and_attempt_references`：finalize 后弱引用可回收（弱引用死亡）
- [x] B.5 **Checkpoint B 审查**：B.1–B.4 全绿

## Checkpoint C — try_aggregate_result publish-before-claim (P0-2)

- [x] C.1 (P0-2) `try_aggregate_result` 改为锁内先 `register_attempt_result(result)` 再 set `_aggregated_to_router=True`（publish-before-claim）；锁顺序 lifecycle→aggregate 不反转
- [x] C.2 (P0-2) 新增测试（patch `register_attempt_result` 卡住发布窗口）：`test_router_finalize_waits_for_claimed_attempt_aggregation` / `test_error_result_published_before_router_report` / `test_usage_cost_published_before_router_report`
- [x] C.3 (P0-2) 新增 `test_stream_finalize_race_aggregates_exactly_one`（断言 `== 1`，非 `<= 1`）
- [x] C.4 (P0-2) 新增 `test_router_report_record_matches_final_in_memory_aggregate`：断言 Reporter 捕获的 Router Record 与最终内存状态一致（status/final_error_category/usage/cost/attempt_count）
- [x] C.5 **Checkpoint C 审查**：C.1–C.4 全绿

## Checkpoint D — regression + archive

- [x] D.1 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [x] D.2 GitHub CI 全部成功（含 live-upstream 0 skipped 证据）
- [x] D.3 **终审**：对照 design Risks 逐项核验（锁顺序无死锁）；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [x] D.4 归档本 change（`openspec archive fix-gateway-router-context-weakref`），不修改已归档历史
