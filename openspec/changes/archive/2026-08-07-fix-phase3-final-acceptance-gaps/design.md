# Design: fix-phase3-final-acceptance-gaps

## Context

Phase 3 Contract/Runtime 主体已冻结并通过 CI + Gateway E2E，但最终验收仍存缺口。
本轮针对四类运行时/契约缺口 + 一次阶段拆分，依据
`docs/llm-observability-phase3-final-acceptance-gap-fix-requirements.md`。

当前代码事实（已核对）：

- `streaming.py:128` `_TerminalFinalizer._finalized = False`；三路径
  `finalize_success` (`:184`) / `finalize_error` (`:209`) /
  `finalize_cancelled` (`:238`) 各自 `if self._finalized: return` 然后
  `self._finalized = True` —— read-check-write **非原子**，无锁。三路径在
  `_publish_and_close` 之前就置位 `_finalized`，但置位与检查之间无临界区，并发
  竞争下两个路径可同时通过检查。
- `router_span.py:735` `register_attempt_result` 在 `_aggregate_lock` 内每次聚合
  都覆盖 `_final_channel_id` (`:748`/`:756`)、`_final_http_status`
  (`:750`/`:754`)、`_final_error` (`:759`) —— 等价于"最后完成的 Attempt 决定
  Router 终态"。Usage/Cost 聚合 (`:764`/`:766`) 与计数 (`:746`/`:752`) 已正确
  sum-all。
- `recorder.py:16` `_TERMINAL_EVENTS` 只含 `attempt.completed/failed` +
  `response.completed/failed`；`stream.completed/cancelled` **不在终态去重集合**
  内，且去重仅按事件名（`_terminal_recorded: set`），无互斥组 ——
  `attempt.completed`+`attempt.failed` 可并存。
- `streaming.py:126` `_duration_ms` 在 wrapper 创建时（响应头时刻）写入 Attempt
  后不再更新；`_publish_and_close` 不覆盖它，故 `gateway.upstream_duration_ms`
  反映响应头耗时而非完整流生命周期。
- Phase 3 文档（Development Spec / CLAUDE.md）将 One-API 接入与 Gateway
  UI/Metrics 列为 Phase 3 DoD，但代码仅有 OneApiAdapter 映射 + 通用 Trace Tree，
  无真实生命周期接入、无 Gateway 专用视图/指标。

约束：telemetry 必须 fail-open；不得引入新 SpanKind；不得侵入 One-API 路由语义；
不得用 `sleep()` 猜测并发时序；不得仅靠改测试绕过并发终态。

## Goals / Non-Goals

**Goals**
- Streaming 终态为原子状态机：同一 Stream 恰好一个 Terminal State，first
  terminal claim wins，失败竞争路径 no-op 且不修改 Span/Router 聚合/Event。
- Hedged/Parallel 下 Router 终态由显式 Winner 决定；Usage/Cost 仍聚合全部
  Attempt；无 Winner 有确定性 Fail-safe。
- Recorder Terminal Event 互斥：同一 attempt/response/stream 组内至多一个终态
  事件。
- Streaming Duration 语义统一为完整上游流生命周期。
- Phase 3 正式拆分为 3.0（本轮冻结）/ 3.1（未来）/ 3.2（未来），文档与实现一
  致。

**Non-Goals**
- 不做 One-API 真实生命周期接入（Phase 3.1）。
- 不做 Gateway 专用 UI 与 Metrics（Phase 3.2）。
- 不改聚合语义（哪些 error 在 Winner 缺省时胜出除外）、不改非流式路径的
  publish-before-claim、不改 SpanKind、不吞业务异常。
- 不通过时间戳猜测终态优先级，不实现"业务错误可升级覆盖"的可升级状态机（首版
  first-claim-wins，未来需要再单独立项）。

## Decisions

### D1 — Streaming 原子终态声明（P0-1，Blocker）

`_TerminalFinalizer` 新增 `_terminal_lock = threading.Lock()` 与
`_terminal_state: Optional[str] = None`，新增：

```
def _claim_terminal(self, state: str) -> bool:
    with self._terminal_lock:
        if self._terminal_state is not None:
            return False
        self._terminal_state = state
        return True
```

三路径改为 `if not self._claim_terminal("success"/"error"/"cancelled"): return`，
**先 claim 再做任何 Span/Router/Event 修改**。`finalized` 属性改为
`self._terminal_state is not None`。`_publish_and_close` 不变（仍是
aggregate→close attempt→close router），但它现在只被赢得 claim 的路径调用。

**优先级：first terminal claim wins。** 不用时间戳、不实现 error>cancel>success
的可升级覆盖（doc §3.3 明确：若产品要求业务异常优先，需专门的可升级状态机，而非
隐式覆盖）。首版冻结 first-claim-wins，并记录该决策。

**替代方案（ rejected）：** 保留布尔 + 在每个 finalize 内用一把大锁包住整个方法
体。Rejected：会把 `_publish_and_close`（含 `Attempt.close()`/`Router.close()`
可能阻塞/回调）整体纳入临界区，扩大锁范围、引入与 `_lifecycle_lock`/
`_aggregate_lock` 的嵌套风险。claim-then-act 只锁 claim，行为体在锁外，锁序不与
既有锁交叉。

### D2 — Hedged Winner 语义（P0-2，Blocker）

**数据模型：** Router 新增
`_selected_attempt_index / _selected_result / _selection_reason`（均受
`_aggregate_lock` 保护）。`AttemptResult` 不新增 `selected` 字段（doc 给了两种方
案，选 Router 显式 `select_winner` —— 业务语义更明确，且不污染 AttemptResult 数据
模型）。

**`select_winner(attempt_index, reason=None) -> bool` 规则：**
- 只允许选择已激活且有 `AttemptResult` 的 Attempt（Router 内部维护
  `_results_by_index: dict[int, AttemptResult]`，由 `register_attempt_result` 填
  充）。
- 默认只允许设置一次；重复选同一 `attempt_index` 幂等返回 True；尝试切换到不同
  index 记录 `gateway.attempt.selected` 事件（reason=`winner_reselected`）并允许
  （首版允许显式切换并记录事件，不静默拒绝）。
- 记录 `gateway.attempt.selected` 事件（属性 `attempt_index`/`channel_id`(哈希)/
  `provider`/`reason`）。

**`register_attempt_result` 改造：** 仍 sum
`_success_count`/`_fail_count`/`_usage_aggregate`/`_cost_aggregate`/`_ttft_ms`，
仍向 `_results_by_index` 存结果，但**不再覆盖** `_final_channel_id`/
`_final_http_status`/`_final_error`。这些 `final_*` 字段改由 Winner 解析：
- 有 Winner → `final_*` 取自 `_selected_result`（channel/http_status/error/finish_reason）。
- 无 Winner 且仅一个 Attempt → finalize 时自动 `select_winner(that_index, reason="auto_single_attempt")`。
- 无 Winner 且多 Attempt → Router final status = ERROR、
  `final_error_category = gateway_internal`、`final_error_type = MissingWinnerSelection`、记录
  `gateway.response.failed`。

**串行 Retry 等价性：** 串行场景业务层最后一个被接受的 Attempt 应由业务层显式
`select_winner`；未显式选择且仅一个成功 Attempt 时，Fail-safe 自动选择该成功
Attempt（优先选 success）。单 Attempt 场景行为与旧"最后完成"等价。

**替代方案（rejected）：** 在 `AttemptResult` 加 `selected: bool`，由 Attempt 自
荐。Rejected：让 Attempt 决定自己是否 Winner 等于让 loser 自认 winner，无法表达
"业务层采用哪一个"；Winner 是 Router/业务层所有权决策，应由 Router 持有。

### D3 — Terminal Event 互斥组（P1-2）

`recorder.py` 新增：

```
_TERMINAL_GROUPS = {
    "attempt": {EVENT_ATTEMPT_COMPLETED, EVENT_ATTEMPT_FAILED},
    "response": {EVENT_RESPONSE_COMPLETED, EVENT_RESPONSE_FAILED},
    "stream": {EVENT_STREAM_COMPLETED, EVENT_STREAM_CANCELLED},
}
```

`record()` 在原"按名去重"之上加"按组去重"：记录某事件后，标记其所在组，同组其
他事件一律拒绝（返回 False）。`stream.completed/cancelled` 同时加入
`_TERMINAL_EVENTS`（原先缺失）。互斥状态 `_terminal_recorded` 改为同时记录"已记
录的事件名"与"已占用的组"，二者皆在 `_span` 非空分支内、fail-open。

**与 D1 的关系：** D1 保证只有赢得 claim 的路径调用 `recorder.stream_completed()`
/`stream_cancelled()`，D3 是第二道防线（即使 D1 失误或 Recorder 被外部直接调
用，互斥组仍阻止并存）。

### D4 — Streaming Duration 语义（P1-1）

`_publish_and_close` 在 aggregate 之前，用
`(terminal_time - attempt._started_at) * 1000` 覆盖 Attempt 的
`gateway.upstream_duration_ms`（`terminal_time` 取 claim 时刻 `time.time()`，已在
`_claim_terminal` 内捕获并存为 `self._terminal_time`）。保留
`gateway.upstream_connect_duration_ms` 不变。非流式 `finish_non_streaming_attempt`
路径不动（其 duration 已是完整请求耗时）。

**替代方案（rejected）：** 拆分为 `upstream_connect_duration_ms` /
`upstream_headers_duration_ms` / `upstream_total_duration_ms` 三字段。Rejected：本
轮只统一语义，不扩字段集（避免契约属性命名大改）；doc §7.2 允许"冻结
`upstream_duration_ms` = 完整上游流生命周期"作为最小修复。未来需要细分再单独立
项。

### D5 — Phase 3 分阶段冻结（P0-3 / P0-4）

本轮正式拆分：
- Phase 3.0 — Gateway Contract & Runtime（本轮冻结）。
- Phase 3.1 — One-API Production Integration（未来 change；本轮只更新文档/DoD）。
- Phase 3.2 — Gateway UI & Metrics（未来 change；本轮只更新文档/DoD）。

更新对象：Phase 3 Development Spec、CLAUDE.md Phase History/Status、Release
Notes、本 change 归档后同步主 spec Purpose。明确"Phase 3.0 只冻结
Runtime/Contract；UI/Metrics/One-API 接入不得在文档表述为已完成"。

**不**创建 `oneapi-production-integration` / `gateway-ui-metrics` 空白 spec 能力
（无实质 requirement，违背 spec 即契约的原则）；3.1/3.2 在 tasks 中以
"future change" 标记。

## Risks / Trade-offs

- **[first-claim-wins 让 cancel 抢先于业务错误]** → 首版接受该语义并显式冻结；
  doc §3.3 允许首版 first-claim-wins。若产品后续要求 error>cancel，单独立项做可
  升级状态机，不在本轮隐式覆盖。
- **[select_winner 未被业务层调用 → 多 Attempt 无 Winner]** → Fail-safe 落到
  `gateway_internal` / `MissingWinnerSelection` 并 ERROR，可观测、可定位，优于隐
  式"最后完成"覆盖成功结果。单 Attempt 自动 Winner，串行常见场景不回归。
- **[`register_attempt_result` 不再覆盖 final_* 影响现有测试]** → 旧测试若依赖
  "最后完成决定 final_channel"需改为显式 `select_winner` 或断言自动 Winner；在
  tasks 中列为 regression 修正项。
- **[D3 互斥组与 D1 双重防护可能掩盖 D1 失误]** → 可接受：防御纵深。测试分别验
  证 D1（claim 竞争）与 D3（Recorder 直接调用互斥），不互相依赖。
- **[duration 覆盖在 cancel 路径取 claim 时刻]** → cancel 时流确已停止消费，
  `terminal_time - start` 即部分消费耗时，符合 doc §7.3
  `test_stream_cancel_total_duration_covers_partial_consumption`。
- **[Phase 3.1/3.2 拆分被误读为"已完成"]** → 文档明确 PENDING；CLAUDE.md
  Current Status 与 Phase History 同步标注；禁止 `v3.0-complete` 标记直至 3.1/3.2
  完成。

## Migration Plan

1. **D1** `_claim_terminal` + 三路径改造 + `finalized` 属性 → 8 个确定性竞争测试
   (`test_stream_terminal_atomicity.py`)。
2. **D3** Recorder 互斥组 + `stream.*` 纳入终态 → 4 个互斥测试
   (`test_terminal_event_exclusion.py`)。
3. **D2** `select_winner` + `register_attempt_result` 改造 + finalize Winner 解析
   + Fail-safe + `gateway.attempt.selected` 事件 → 9 个 Hedged 测试
   (`test_hedged_attempt_winner.py`)；修正依赖旧 final_* 覆盖语义的现有测试。
4. **D4** `_publish_and_close` 覆盖 `upstream_duration_ms` → 4 个 duration 测试
   (`test_stream_duration_semantics.py`)。
5. **D5** 更新 Development Spec / CLAUDE.md / Release Notes / DoD；归档后同步主
   spec Purpose。
6. 全量回归：`.venv/bin/python -m pytest sdk/tests/ tests/ -q`；GitHub CI 全绿；
   Real E2E 0 skipped；Phase 2.1–2.5 regression 全绿。
7. 冻结 Phase 3.0；archive 本 change（不修改已归档历史）。

回滚：D1/D2/D3/D4 均为代码级改动，git revert 即可；D5 文档改动单独 revert。无数据
库/外部依赖迁移。

## Open Questions

- `select_winner` 切换 Winner（已选 A 又选 B）首版是否允许？本设计选择"允许并记
  录 `gateway.attempt.selected` 事件（reason=`winner_reselected`）"。若审查认为
  应禁止切换，改为返回 False 并记录拒绝事件 —— 在 tasks Checkpoint C 审查时定。
  （不阻塞实施，首版按"允许+记录"实现。）
