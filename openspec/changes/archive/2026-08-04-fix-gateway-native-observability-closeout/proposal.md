# Proposal: fix-gateway-native-observability-closeout

## Why

Phase 3 (Gateway Native Observability, commit `6c00760`) 的审查结论为 **CHANGES REQUIRED**：实现范围完整但核心语义不正确。存在 8 个 P0 缺陷（无 SDK 时 TraceID 非法、Trace Origin 标记错误、Attempt 关闭清空 Router Context、Streaming 终态未聚合到 Router、Router Finalize 遗留 Open Attempt、Real E2E 假阳性、Usage 跨进程所有权不成立、Event 泄露原始 Channel ID）和 7 个 P1 缺陷（Attempt Index 重复、Cost 未用 resolved model、Event Contract 未接入生命周期、Association 字段不全、Span Attribute 未过 PrivacyGuard、Sampling 传播测试不足、OpenSpec 提前归档）。详见 `docs/llm-observability-phase3-rework-bug-fix-requirements.md`。本 change 不修改已归档历史，而是在其上返工，使 Phase 3 达到可冻结标准。

## What Changes

**P0 — 核心语义修复**

- **Trace 身份（P0-1/P0-2）**：Router Parent Resolver 返回 `ResolvedGatewayParent{trace_id, parent_span_id, origin, upstream_trace_present}`；无 SDK/无 traceparent 时生成合法 32 位十六进制 TraceID（非全 0、连续请求唯一）；`gateway.trace_origin` 冻结为 `sdk` / `remote` / `gateway` 三值，`gateway.upstream_trace_present` 与之严格一致。
- **Context 分槽（P0-3）**：Gateway Context 拆为 Router Slot 与 Attempt Slot；Attempt 关闭只清 Attempt Slot，Router Context 仅 Router 终态才清理；cross-context reset 不连带清理 Router Slot。
- **Streaming 终态（P0-4）**：拆分 `finish_non_streaming_attempt` / `finalize_streaming_attempt`；禁止在响应头阶段聚合成功；Streaming Success/Error/Cancel 三种终态构造最终 `AttemptResult` 并一次性聚合到 Router，Router 与 Attempt 状态一致；TTFT 从真实上游请求开始计算，忽略 keepalive / 空 chunk / metadata-only / usage-only / `[DONE]`。
- **Open Attempt 清理（P0-5）**：Router 显式维护 `_open_attempts` 注册表；`finalize()` 对所有遗留 Attempt 调用幂等、fail-open 的 `force_close()`（不覆盖已有业务错误、清理 Registry/Context、上报终态）。
- **Real E2E（P0-6）**：新增 `sdk/tests/gateway_observability/test_real_gateway_e2e.py`，覆盖成功/Retry/Fallback/Streaming Success/Streaming Cancel/无 SDK Trace/sampled=0/隐私；统一 CI 变量 `GATEWAY_E2E_API_KEY/BASE_URL/MODEL`；可信分支缺 Secret 即失败、Fork PR 跳过整个 Job；用 `scripts/redact_ci_secrets.py` 替换单引号 sed 脱敏；CI 断言运行数 > 0 且必需 E2E skipped = 0。
- **Usage 所有权（P0-7）**：**BREAKING**（语义层面）删除"SDK LLM Usage 必须等于 Router Aggregate"的 ContextVar 回写强制语义。Attempt 拥有单次真实请求的 Usage/Cost；Router 汇总所有 Attempt（含失败重试）；SDK LLM 只记录调用方收到的逻辑响应 Usage；Core/UI 从 Trace 树推导 Retry Waste。
- **Channel ID 隐私（P0-8）**：Route/Attempt/Fallback Event 中的 channel ID 全部经 `PrivacyGuard.hash_channel_id()`；`gateway.fallback.selected` 冻结为 `from_channel_id` + `to_channel_id` + `reason`；任何 Telemetry 不得出现原始 Channel ID。

**P1 — 完整性与加固**

- **Attempt Index（P1-1）**：默认由 `router.allocate_attempt_index()` 递增分配且并发安全；显式重复值重映射并告警；非法值回退自动分配。
- **Cost（P1-2）**：CostCalculator 必须使用 `attempt.resolved_model`；价格表单位冻结为 USD / 1M tokens（`input_usd_per_1m_tokens` / `output_usd_per_1m_tokens`）；无价格时 `cost.source = unpriced`；cache 显式 cost 保留。
- **Event 生命周期（P1-3）**：`route_selected`、`attempt_started/completed/failed`、`response_completed/failed` 真正接入 Runtime 生命周期；每个终态 Event 最多一次、在 span end 前写入、经 PrivacyGuard。
- **Association（P1-4）**：Router 完整写入 `user_id` / `session_id` / `message_id` / `app_name` / `business_scenario`（命名单一，与现有 Span Record 一致），优先级：显式值 > Remote Header/Baggage > None。
- **Attribute Privacy（P1-5）**：新增统一入口 `set_gateway_attribute(span, key, value, privacy_guard)`——字段白名单、值脱敏、长度限制（字符串 ≤512B、request_id/route/reason ≤256B、provider/model ≤128B）、默认拒绝未知 key、fail-open。
- **Sampling 传播（P1-6）**：提供 `inject_downstream_trace_headers(router, attempt)`，下游 traceparent 的 parent 为 Attempt span_id，sampled=0 时 `trace_flags=00`。

**流程（P1-7）**：本 change 即返工 change；分三个检查点审查（Trace+Context+Attempt 生命周期 → Streaming+Usage/Cost+Privacy → Real E2E+CI+回归），三层证据（代码审查、CI 绿色、Real E2E 0 skipped）齐备后才归档。

## Capabilities

### New Capabilities

（无 —— 本 change 全部是对既有 capability 需求的修正）

### Modified Capabilities

- `gateway-observability-contract`：冻结 Trace Origin 三值语义与 `upstream_trace_present` 一致性；冻结 `gateway.fallback.selected` 事件字段（from/to，已哈希）；冻结 Streaming Success/Error/Cancel 的 Router/Attempt 状态一致性契约；冻结 Usage 所有权（Attempt 单次 / Router 汇总 / LLM 逻辑）；冻结 Association 字段集与单一命名。
- `gateway-observability-runtime`：Router Parent Resolver 必须生成合法 TraceID；Context 分槽语义；Open Attempt 注册表与 `force_close()`；Streaming finalize 拆分与一次性聚合；Attempt Index 分配规则；`set_gateway_attribute` 统一隐私入口；Cost 使用 resolved model 与 1M-token 价格单位；`inject_downstream_trace_headers` 下游传播；真实 Gateway E2E 与 CI 门禁要求。

## Impact

- **代码**：`sdk/python/llm_observability/gateway_observability/`（context、router_span、attempt_span、runtime、streaming、recorder、registry、privacy、propagation、aggregation、cost）、`instrumentation/openai.py`、`integrations/langchain/llm_span.py`、`integrations/oneapi/adapter.py`、`.github/workflows/ci.yml`。
- **新增**：8 个测试文件（trace_identity / context_lifecycle / stream_terminal_state / open_attempt_cleanup / channel_privacy / usage_ownership / downstream_propagation / real_gateway_e2e）、`scripts/redact_ci_secrets.py`。
- **API 语义变化**：删除 LLM Usage = Router Aggregate 的 ContextVar 回写（跨进程部署不成立）；下游可按显式 Header 协议（`x-llm-obs-*`）选择性回传，另行设计。
- **CI**：`gateway-real-e2e` job 改为运行新 E2E 文件并加 skipped=0 门禁。
- **回归约束**：Phase 2.1～2.5 全部测试必须通过；禁止修改 One-API 路由行为、禁止新增 SpanKind、禁止吞业务异常。
