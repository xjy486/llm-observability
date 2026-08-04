# Tasks: fix-gateway-forceclose-state-consistency

依据 proposal/design/spec 实施。冻结阻塞项（Blocker 1/2）必须全绿才能视为可冻结。

## Checkpoint A — Blocker 1 (force_close preserves aggregated outcome)

- [x] A.1 (Blocker 1) `force_close()` 重排：先 `_closed`→no-op；再 `_aggregated_to_router`→只 `close()`（保留已记录 OK/ERROR 状态，不写 gateway_internal、不重聚合）；否则才 set gateway_internal + 聚合 + close
- [x] A.2 (Blocker 1) 测试 `test_force_close_aggregation.py` 新增：`test_finalized_success_but_open_force_close_keeps_both_ok` / `test_finalized_error_but_open_force_close_keeps_same_error` / `test_finalized_open_attempt_no_duplicate_aggregation` / `test_finalized_open_attempt_no_duplicate_report`
- [x] A.3 **Checkpoint A 审查**：A.1–A.2 全绿后进入 Checkpoint B

## Checkpoint B — Blocker 2 (closed Router rejects new registration)

- [x] B.1 (Blocker 2) `Router.close()` 在 `_open_attempts_lock` 内设置 `_closed=True`，与快照+clear 同一临界区
- [x] B.2 (Blocker 2) `register_open_attempt` 返回 bool：`_closed` 为 True 时拒绝（no-op，返回 False）
- [x] B.3 (Blocker 2) `AttemptSpan.start()`：注册返回 False 时走 fail-open no-op：跳过 registry 注册、跳过 set_attempt ContextVar、标记 span 不上报；不抛异常
- [x] B.4 (Blocker 2) `RouterSpan.attempt()`：`_closed` 时返回 no-op AttemptSpan（不分配 index/不进 _attempts）
- [x] B.5 (Blocker 2) 测试 `test_open_attempt_cleanup.py` 新增：`test_attempt_start_after_router_close_is_noop` / `test_attempt_register_racing_router_finalize_no_leak` / `test_router_finalize_blocks_post_snapshot_registration` / `test_concurrent_attempt_start_and_finalize_registry_zero`
- [x] B.6 **Checkpoint B 审查**：B.1–B.5 全绿后进入 Checkpoint C

## Checkpoint C — non-blocking + regression + archive

- [x] C.1 (非阻塞) HTTP streaming-cancel E2E 改为确定性：服务器端 `threading.Event` 同步代替 `sleep`，断言 `gateway.stream.cancelled` 事件触发
- [x] C.2 (非阻塞) Association 顶层字段（user_id/session_id/message_id/app_name/business_scene）加字节长度限制（≤256B）+ 控制字符剥离；单测覆盖
- [x] C.3 全量回归：gateway_observability 套件 + Phase 2.1–2.5 + 归档 closeout 测试全绿
- [ ] C.4 GitHub CI 全部成功
- [ ] C.5 **终审**：对照 design Risks 逐项核验；三层证据（代码审查 / CI 绿 / E2E 0 skipped）齐备
- [ ] C.6 归档本 change（`openspec archive fix-gateway-forceclose-state-consistency`），不修改已归档历史
