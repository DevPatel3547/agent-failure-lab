# Experimental methodology

## Task and treatments

Each episode asks for exactly one ticket titled `Investigate dropped checkout events`. A fresh isolated world and operation id are used per episode. The agent can create, look up the operation's tickets, wait, or finish with succeeded/failed/unknown and a ticket id.

| Mode | Live model | Scripted demonstration |
|---|---|---|
| Plain | Base instructions; direct tools | Retry after ambiguity; accept positive acknowledgements |
| Guided | Base plus recovery instructions; direct tools | Add one lookup after an uncertain write/restart |
| Guarded | Same base instructions as plain; recovery gateway | Same script as plain; recovery gateway |

The scripted guided policy is intentionally simple and is **not** a proxy for the live recovery prompt. Its purpose is to demonstrate how one read-back can still fail under stale or delayed state. It is not a strong hand-designed baseline. Comparisons to a production recovery implementation require a stronger baseline and realistic connector workloads.

The base prompt already asks for evidence before claiming success. Only guided adds specific recovery guidance. Guarded substitutes a stable system key and validates the requested title. It changes tool behavior and the resulting history, so treatment differences describe the whole system, not just model reasoning quality.

## Hidden information and grading

The model sees the public provider contract (key/lookup support, lifetime, consistency, clock units) but not which fault will occur. The simulator records committed effects independently of public responses. After the agent finishes or exhausts its steps, queued writes settle within a bounded horizon before grading.

- **Duplicate effects:** more than one committed ticket for the logical operation.
- **Missing:** no ticket committed, including legitimate authorization/rejection cases. This is task noncompletion, not automatically unsafe behavior.
- **Wrong payload:** an effect has the wrong title.
- **False success:** the agent reports success without exactly one correctly titled effect and the matching id.
- **False failure:** the agent reports failure although at least one effect committed.
- **Unknown:** the final status remains unknown, including step or API-budget exhaustion. Intentional abstention and exhausted execution are separately distinguishable through `execution_status`.
- **Task success:** exactly one correct effect and a correct success report.

Metrics can overlap. A duplicate with a success claim counts as both duplicate and false success. An unresolved write may have committed exactly once. Report these dimensions together; fewer duplicates alone is not proof of a better system.

## Reproducibility

Fixtures and their canonical SHA-256 hashes are recorded in every result. Experiment metadata includes package version, provider/model, fixture-set hash, modes, trial count, step limit, schedule seed, and limits. Scenario/mode/trial combinations run in shuffled seed-controlled order. Logical time and a recorded action sequence are deterministic; provider outputs are not promised to be deterministic.

`replay` reconstructs the scenario and actions without contacting a model. All 72 default scenario/mode combinations are tested for matching replay metrics. Operation ids and wall-clock timings change; this is effect-metric replay, not byte-identical trace replay.

`compare` requires the same fixture set, provider/model, modes, trials, step limit, schedule seed, and complete episode identities. It flags newly introduced bad metrics, lost successful completions, and execution errors/budget exhaustion. Package versions may differ so code changes can be compared. Check prompt/provider version changes yourself: named model ids may be moving aliases. This is a paired regression gate, not a significance test.

## Limits on conclusions

The 24 fixtures are hand-authored failure cases, not sampled incidents or a representative benchmark. Repeated scripted trials repeat deterministic mechanisms and add no independent statistical evidence. A rate such as “1/24 duplicates” describes this selection only. Do not attach confidence intervals that imply a representative population.

Live runs are bounded single-operation agents with full public history resent each turn. No persistent private reasoning, parallel tool calls, framework-specific memory, multi-agent interaction, real user ambiguity, or free-form tool schemas are evaluated. Prompt length is bounded and visible; exceeding it stops rather than silently truncating history.

The simulator serializes service state and controls all clocks. The example HTTP path tests a genuine transport boundary, but the standard experiment loop calls the simulator in process. The GitHub connector has no independent effect grader integrated into the benchmark. Real provider timeouts and authorization semantics require separate validation.

API adapters record token usage when returned. Missing or malformed usage is counted, not interpreted as zero-cost execution. Output token and request caps are not dollar budgets. No live-model performance claims are made by the bundled release.

## Useful next evidence

A credible extension is a minimized failure from an actual owned connector: document its observed request/response behavior, add a reproducing fixture, keep provider uncertainty explicit, and show the fix plus any availability cost. For live models, preserve exact model ids, prompts, settings, raw report exports, and costs; separate independent trials from deterministic replays.

## v0.2 reference baseline, exploration, and wire experiment

The optional `reference` mode uses a deterministic contract-aware policy: stable keys, reconciliation, full declared rate-limit waits, conflict detection, and explicit unknown outcomes. It conservatively avoids uncertain replay with finite key lifetimes because it cannot establish a sufficient lifetime from the public history. It is stronger than the original guided script and is always labeled scripted. Live-model experiments cannot mix this mode into their treatment set.

`campaign` runs plain/reference/guarded over seeded independent parameter combinations. The generator version, seed, case count, exact fixtures, and all results are saved. This expands coverage but does not produce representative incident frequencies. `minimize` performs bounded complement-based delta debugging of recorded actions, followed by a deletion check when the budget allows. It preserves the target violation plus the duplicate/wrong-payload signature. It does not infer root cause, optimize scenario parameters, or rerun a model.

`wire-demo` compares those same three scripted policies with actual socket failures in a local proxy and separate healthy service processes. Service business state still uses the controlled ticket fixture, including its logical clock. The physical transport is real; workload semantics remain synthetic. Proxy evidence and committed effects are separate. The report exposes both without claiming the proxy can infer commit status.

Portable `.json.gz` evidence is accepted by `render`, `minimize`, `replay`, and `compare` with a 50 MB expanded-size limit. The reduced artifact is separately content-hashed and validated. A replay fixes the original final action as well as tool actions; changing a gateway does not retroactively change an old caller's recorded claim.
