## ADDED Requirements

### Requirement: SDK LLM span usage aggregates gateway router usage

When a Gateway Router span exists for an SDK LLM logical call, the SDK LLM span SHALL record the Router aggregate Usage (sum over all attempts, including failed ones) as its final Usage, and SHALL NOT record only the final successful Attempt's Usage. The SDK LLM span remains the owner of the logical call's final Usage and Timing; the Router remains the owner of the per-gateway decision aggregate.

#### Scenario: LLM usage equals router aggregate after retry

- **WHEN** an SDK LLM call routes through a gateway whose Router aggregated Usage across a failed attempt and a successful attempt
- **THEN** the SDK LLM span's final Usage equals the Router aggregate Usage
- **AND** the LLM span does not carry only the successful attempt's Usage

#### Scenario: LLM span without router keeps its own usage

- **WHEN** an SDK LLM call has no Gateway Router span
- **THEN** the SDK LLM span records its own directly-measured Usage and is unchanged by this requirement
