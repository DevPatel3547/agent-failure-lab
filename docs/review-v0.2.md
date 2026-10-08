# v0.2 engineering review

Review dimensions follow security, performance, correctness, and maintainability. Ratings describe this local workbench's intended scope, not production certification.

| Dimension | Assessment | Evidence and limits |
|---|---|---|
| Correctness | Material defect found and fixed | [Gateway](../agent_failure_lab/gateway.py): rejection/rate limiting of a retry no longer erases prior uncertainty. Before/after evidence and regression in failure-study.md. |
| Security | Appropriate local boundaries; not production hardened | [Proxy](../agent_failure_lab/wire.py): fixed loopback upstream, bounded bodies/timeouts, no dynamic forwarding destination, and no header/query/payload logging. No authentication, TLS interception, or sandbox claim. |
| Performance | Bounded for local investigation | [Reducer](../agent_failure_lab/explore.py): evaluation budget and memoized candidates; campaigns capped at 1,000 cases. No throughput or production-scale performance benchmark. SQLite world traces are serialized JSON and are unsuitable for unbounded workloads. |
| Maintainability | Inspectable modules and regression evidence | Protocol, recovery, transport, baseline, exploration, grading, report, and storage remain separate. Zero runtime dependencies and CI across four Python versions. |

## Specific findings

- **Fixed, high:** logical operation marked rejected after an uncertain earlier commit. See `RecoveryGateway._persist` and the authorization/lost-acknowledgement regression.
- **Fixed, medium:** multiple visible effect ids accepted as confirmation. Recovery now returns conflict; the stronger baseline abstains.
- **Fixed, medium:** truncated HTTP response bodies could escape transport error handling. `JSONTransport.request` catches HTTP protocol exceptions and does not retry automatically.
- **Fixed during upgrade, medium:** response compression metadata must remain intact when proxying unchanged bodies. A gzip round-trip regression covers that behavior.
- **Open scope limit:** finite provider-key TTLs cannot establish safe retry deadlines on an arbitrary real network. Existing lifetime checks are conservative in logical time; no universal distributed guarantee is made.
- **Open scope limit:** the plain caller behind the gateway may repeatedly request recovery until the step bound. Unknown outcomes remain visible; a production escalation policy is outside the current workbench.

Positive properties retained from v0.1: intent is durable before dispatch; confirmation cannot be downgraded by a late uncertain response; hidden effects are excluded from model prompts; grades compare authoritative effects to final claims; and report data is rendered as text rather than executable HTML.
