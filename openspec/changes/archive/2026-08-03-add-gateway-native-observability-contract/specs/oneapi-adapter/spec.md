# One-API Adapter Specification

## Purpose

Defines the One-API glue-layer adapter: how One-API request/response/channel/route/retry/usage concepts map to the gateway contract, and the boundary constraints that keep the adapter from modifying One-API core behavior.

## ADDED Requirements

### Requirement: One-API request and channel mapping

The adapter SHALL map a One-API request token to association fields (`user`, `session`, `app`), a One-API Channel to `gateway.channel_id` / `gateway.channel_type` / `gateway.provider`, model mapping to `gateway.requested_model` / `gateway.resolved_model`, and relay mode to `gateway.protocol`. Channel IDs SHALL be hashed before recording.

#### Scenario: Request token and channel mapped

- **WHEN** a One-API request carries a request token and selects a channel
- **THEN** the adapter produces a `GatewayRequestContext` with the association fields and a `RouteDecision` with provider, channel_id, channel_type, requested_model, and resolved_model
- **AND** the recorded channel_id is the hash of the internal ID

### Requirement: One-API retry, fallback, quota, and response mapping

The adapter SHALL map One-API retry to a new Attempt span plus a `gateway.retry.scheduled` event, fallback to a `gateway.fallback.selected` event plus a new Attempt, quota to Usage/Cost, and the upstream response to the Attempt result. Streaming responses SHALL be wrapped so spans cover the full stream lifecycle.

#### Scenario: Retry and fallback mapped to attempts

- **WHEN** One-API retries a failed channel and then falls back to another channel
- **THEN** the adapter emits a `gateway.retry.scheduled` event and a new Attempt for each real upstream request
- **AND** a single `gateway.fallback.selected` event records the from/to channel transition

#### Scenario: Usage and cost mapped from quota response

- **WHEN** an upstream response carries usage/quota data
- **THEN** the adapter produces a `NormalizedUsage` and the runtime records Usage and Cost on the Attempt

### Requirement: One-API adapter boundary constraints

The One-API adapter SHALL NOT change Channel selection, modify retry counts, modify timeouts, modify quota, or catch and swallow business exceptions. It SHALL only extract fields, map concepts, invoke events, and normalize state. The adapter SHALL NOT persist, generate TraceIDs, report over HTTP, or alter routing.

#### Scenario: Adapter does not mutate routing

- **WHEN** the adapter processes a One-API request
- **THEN** the One-API channel-selection and retry logic executes unchanged
- **AND** the adapter's only effects are telemetry extraction, mapping, and event recording

#### Scenario: Adapter does not swallow business exceptions

- **WHEN** an upstream request fails with a business exception
- **THEN** the exception propagates to One-API unchanged and the adapter records it as an Attempt failure with a classified error category
