# Review this project in ten minutes

The narrow claim: a tool response is not necessarily the committed effect, and recovery code should preserve that distinction across ambiguous writes, retries and restarts. This project supplies inspectable counterexamples and a reusable test boundary.

## 1. Inspect a real failure and its fix

Read the [authorization-revocation counterexample](failure-study.md#the-bug-in-v01). A previous version reported failure after a committed write lost its acknowledgement and a later retry was rejected. Compare the retained before/after artifacts. The fix preserves uncertainty; it does not manufacture a successful outcome.

Relevant implementation: `RecoveryGateway._persist` in `agent_failure_lab/gateway.py`. Regression: `test_rejected_retry_does_not_establish_failure_of_prior_write` in `tests/test_upgrade.py`.

## 2. Reproduce the transport boundary

Run `python3 -m agent_failure_lab wire-demo`. Requests cross a real HTTP proxy into separate service processes; the caller's journal and authoritative service ledger are separate databases. Inspect the public report's HTTP evidence panel and reveal the hidden committed effects.

Relevant implementation: `agent_failure_lab/wire.py`, `transport.py`, and `tests/test_upgrade.py`. The fault proxy's network logs alone do not prove whether a business operation committed.

## 3. Check the limits of the result

The strong deterministic reference matches the gateway on effect outcomes in the published eight-case HTTP set. The default named suite still contains a provider-redelivery case the gateway cannot prevent. Uncertain writes can remain unresolved. Read the [complete failure study](failure-study.md), including negative results.

## 4. Verify it works in an external runtime

Install the optional `.[agents]` extra and run `python3 tests/sdk_check.py`. This uses the real OpenAI Agents SDK Runner and function-tool execution with scripted inference, not another lab-specific runner. The test covers simultaneous calls with distinct SDK call IDs for the same logical operation. [Integration report and limitations](agents-sdk.md).

## 5. Audit the next model experiment

Read [evaluation.md](evaluation.md) and `tests/test_evaluation.py`: frozen treatment schedule, source fingerprint, per-request durable reservations, full-schedule resume, explicit error/missing denominators, and no automatic rerunning of bad outcomes. Protocol doubles are clearly marked. No live model results are currently claimed.

## Questions this work does not answer

Does a particular frontier model fail frequently on production tasks? Does this gateway improve outcomes for an outside team? Is its cost justified at production scale? Those require real model experiments, independently sourced workloads and external use. The repository is engineering evidence for the mechanisms above, not a substitute for those measurements.
