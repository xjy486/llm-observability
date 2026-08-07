# Phase 3.0 — Gateway Contract & Runtime: Release Notes

> 阶段：Phase 3.0 — Gateway Native Observability (Contract & Runtime)
> 状态：COMPLETE / FROZEN
> 相关 change：`openspec/changes/fix-phase3-final-acceptance-gaps/`（最终验收缺口修复）

## 概述

Phase 3 正式拆分为三个可独立冻结的子阶段：

- **Phase 3.0 — Gateway Contract & Runtime**（本轮，冻结）
- **Phase 3.1 — One-API Production Integration**（未来 change，PENDING）
- **Phase 3.2 — Gateway UI & Metrics**（未来 change，PENDING）

本轮（`fix-phase3-final-acceptance-gaps`）补齐 Phase 3.0 最终验收缺口并冻结
3.0。One-API 真实生命周期接入与 Gateway 专用 UI/Metrics 分别推迟到 3.1 / 3.2，
不再计入 3.0 DoD。

## 本轮新增 / 修复

### P0-1 Streaming Terminal 原子状态机（Blocker）
- `_TerminalFinalizer` 新增 `threading.Lock` 保护的 `_claim_terminal(state)`
  原子终态声明；`finalize_success` / `finalize_error` / `finalize_cancelled`
  改为先 claim、claim 失败则 no-op。
- 语义冻结为 **first terminal claim wins**：不通过时间戳猜测、不隐式覆盖。
  并发竞争下同一 Stream 恰好一个 Terminal State，Router/Attempt 终态一致。

### P0-2 Hedged / Parallel Attempt Winner 语义（Blocker）
- 区分「全部 Attempt 聚合」与「业务最终 Winner」：
  - Usage/Cost/success_count/fail_count 仍聚合所有 Attempt（含失败与 loser）。
  - Router final status / channel / HTTP status / error 由显式 Winner 决定。
- 新增 `RouterSpan.select_winner(attempt_index, reason=None) -> bool`：
  仅可选已激活且已有 `AttemptResult` 的 Attempt；幂等；切换 Winner 记录事件。
- 确定性 fail-safe：无 Winner 且恰一个成功 → 自动选择该成功 Attempt；
  无 Winner 且单一 Attempt → 自动选择；无 Winner 且多 Attempt（歧义）→
  Router ERROR，`final_error_category = gateway_internal`，
  `final_error_type = MissingWinnerSelection`。
- `register_attempt_result` 不再每次覆盖 `final_*`。
- 新增事件 `gateway.attempt.selected`（属性 `attempt_index` / `channel_id`(哈希) /
  `provider` / `reason`）。

### P1-1 Streaming Duration 字段语义
- Streaming 终态时用 `terminal_time - attempt_start_time` 覆盖
  `gateway.upstream_duration_ms`，冻结为完整上游流生命周期（不再保留响应头
  时刻的 duration 作为总耗时）；`upstream_connect_duration_ms` 不变。非流式
  语义不变。

### P1-2 Terminal Event 互斥组
- `GatewayEventRecorder` 新增互斥组 `attempt` / `response` / `stream`：同组记录
  一个事件后，其余事件全部拒绝（不仅仅按事件名去重）。
- `gateway.stream.completed` / `gateway.stream.cancelled` 纳入终态去重集合与
  `stream` 互斥组。

### Phase 3 分阶段冻结
- 更新 Phase 3 Development Spec（§2.1 阶段拆分、§28 Phase 3.0 DoD）、
  CLAUDE.md（Current Status + Phase History）、本 Release Notes。
- 不创建 `oneapi-production-integration` / `gateway-ui-metrics` 空白 spec 能力；
  3.1 / 3.2 由各自 change 在启动时建立实质 requirement。

## 新增测试

```text
sdk/tests/gateway_observability/test_stream_terminal_atomicity.py    # 10
sdk/tests/gateway_observability/test_terminal_event_exclusion.py     # 6
sdk/tests/gateway_observability/test_hedged_attempt_winner.py        # 10
sdk/tests/gateway_observability/test_stream_duration_semantics.py    # 4
```

所有并发竞争测试使用 `threading.Event` / `threading.Barrier` / `asyncio.Event`
制造确定性竞争窗口，不依赖 `sleep()` 猜测时序。

## 兼容性

- `select_winner` 为新增 API；`final_*` 由 Winner 决定是行为收紧，但单 Attempt
  串行场景由 auto-single fail-safe 自动选择，行为与旧「最后完成」等价。
- 无新 SpanKind；telemetry 保持 fail-open；不侵入 One-API 路由语义。
- Phase 2.1–2.5 与全部已归档 closeout 测试保持全绿。

## 验收

- 全量回归：`.venv/bin/python -m pytest sdk/tests/ tests/ -q` 全绿（~1000+ 通过，
  1 skipped 为 live-upstream E2E）。
- Gateway Runtime E2E（HTTP harness）0 skipped。
- 对照 `docs/llm-observability-phase3-final-acceptance-gap-fix-requirements.md`
  §13 验收矩阵逐项确认。

## 后续阶段（不在 3.0 内）

- **Phase 3.1**：真实 One-API 生命周期接入（middleware/hooks/lifecycle/
  streaming/bootstrap）+ 容器化 One-API E2E。
- **Phase 3.2**：Router Detail / Attempt Timeline / Route Decision / Cost
  Breakdown / Gateway Filters / Gateway Metrics / Retry-Hedge Waste Cost。
