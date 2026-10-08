# From a lost acknowledgement to a reproducible failure

## The bug in v0.1

The preserved run at commit `6dac9dbad37e4293ba454ab2fcac4549038efe63` follows this sequence:

1. At service tick 1, a create request commits ticket T-1. Its acknowledgement is lost.
2. Lookup is unavailable under this provider contract.
3. The gateway retries with the same key. Authorization has expired; the provider rejects the new request before considering replay.
4. The gateway records that rejection as the operation result. The scripted caller reports failed.
5. The independent ledger contains T-1. The grader flags **false failure**.

The mistake was conflating a request result with the logical operation's history. The second request did not commit; that says nothing conclusive about the first request.

[Before trace](evidence/revocation-before-fix.json) · [After trace](evidence/revocation-after-fix.json) · [Fix](../agent_failure_lab/gateway.py) · [Regression tests](../tests/test_upgrade.py)

## The correction and its cost

The gateway now preserves prior uncertainty when a repeated dispatch returns rejection or rate limiting. It cannot move that operation to a terminal rejection or to the “safe new attempt” state solely on that evidence. A lookup returning multiple effects also produces conflict rather than a confirmation.

In the new run, one effect still exists, but the caller reports unknown. This fixes the false-failure claim; it does **not** recover enough evidence to report success. The plain scripted caller keeps asking until its step bound is reached, so the trace also exposes repeated unsuccessful recovery attempts. A production escalation policy would stop or request human reconciliation sooner. The reference policy demonstrates explicit abstention.

The code review also found that truncated HTTP bodies could raise an unhandled protocol exception. The transport now converts that condition to a bounded, non-retried transport failure, with a real truncated-response test.

## Real transport evidence

`wire-demo` runs a healthy local ticket service in a separate process and injects failures in a reverse proxy. It uses a separate durable client journal and grades the service ledger after the caller finishes. No service-level failure flag is used to simulate the wire error.

Four conditions are tested with and without provider keys, under plain retry, contract-aware reference, and guarded retry: **24 episodes**. The conditions are disconnect-before-forwarding, suppress-response-after-upstream-response, corrupt-response-JSON, and 503-before-forwarding.

| Policy | Correct completion | Duplicate effects | False success | Unknown | Missing |
|---|---:|---:|---:|---:|---:|
| Plain | 4 / 8 | 4 | 4 | 0 | 0 |
| Reference | 6 / 8 | 0 | 0 | 2 | 2 |
| Guarded | 6 / 8 | 0 | 0 | 2 | 2 |

The reference and gateway have identical effect outcomes in this small experiment. Both sacrifice completion for ambiguous unkeyed requests that never reached the service. This is a useful negative result: adding a journal does not remove an API's information limits.

Proxy records distinguish a forwarding **attempt**, receipt of an upstream response, and completion of a downstream response write. None alone proves that the intended external effect committed or that the application received the reply. The independent service ledger supplies the effect evidence. Concurrent request occurrences are assigned by arrival order; the proxy is not a deterministic network scheduler.

[Full wire dataset](evidence/v0.2-wire.json.gz) · [Portable interactive report](index.html)

## Seeded exploration

The published campaign uses 64 generated scenarios, seed 7, a 20-step bound, and three policies: **192 episodes**. The generator varies visibility, key support/lifetime, commit delay, failure count, restart, authorization, and rate-limit backoff.

| Policy | Correct completion | Duplicate effects | False success | False failure | Unknown | Missing |
|---|---:|---:|---:|---:|---:|---:|
| Plain | 17 / 64 | 24 | 24 | 1 | 0 | 22 |
| Reference | 24 / 64 | 0 | 0 | 0 | 28 | 27 |
| Guarded | 26 / 64 | 0 | 0 | 0 | 26 | 27 |

Zero duplicates in this finite campaign is not a safety proof. The named fixture suite contains provider redelivery without keys, which the gateway cannot prevent. These independent parameter combinations are a coverage exploration, not a realistic joint distribution. No significance or population claim is supported by these counts.

[Full campaign dataset](evidence/v0.2-campaign.json.gz) · [Generator](../agent_failure_lab/explore.py)

## Reducing the investigation

The longest duplicate-producing plain trace in that campaign contains five actions. Bounded delta debugging reduces it to two create actions while retaining the duplicate-effect violation. Deleting either remaining action removes the violation under the fixed scenario.

```sh
python3 -m agent_failure_lab reproduce docs/evidence/reduced-duplicate.json
# Expected exit code: 1; failure_observed: true.
```

The reduction is **1-minimal under action deletion**, not a globally minimal explanation. The scenario parameters remain fixed. A generated regression test asserts the desired absence of duplicates and intentionally fails until the behavior is fixed.

Replay preserves all recorded actions, including an old final claim. Replaying v0.1's recorded “failed” action still produces a false-failure score even with the gateway fix. To evaluate whether changed observations improve the caller's decisions, rerun the policy against the same scenario. A regression test explicitly distinguishes these two operations.

## Reproduce and inspect

```sh
python3 examples/build_evidence.py
python3 -m agent_failure_lab render docs/evidence/v0.2-wire.json.gz
python3 -m agent_failure_lab render docs/evidence/v0.2-campaign.json.gz
```

The [manifest](evidence/manifest.json) hashes the published compressed datasets and reduced case. Runs use fresh operation ids and timestamps, so regenerated files will have different hashes even when deterministic effect counts match. Inspect the complete data before interpreting an aggregate.

No model API calls, real GitHub issue writes, production incidents, user adoption, or claimed novel algorithms are part of this evidence.
