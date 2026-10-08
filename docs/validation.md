# Validation record

This release separates an implemented feature from a live-service claim.

## Checked locally

- All 71 backend tests passed locally on Python 3.12, including real loopback HTTP requests, subprocess termination after commit, concurrent recovery/backoff, all-fixture replay, provider protocol validation, incomplete-run accounting, GitHub pagination, and report escaping. The added tests cover real proxy faults, isolated service processes, strong reference policies, compressed reports, reduction minimality, and the v0.1 false-failure regression.
- Chromium acceptance checks passed for the 72-case default report and the 24-case real-HTTP report, including the reference filter and transport evidence panel, strategy/search/outcome filters, explicit world-state reveal, trace scrubbing, JSON download, injected HTML displayed as text, zero external report requests, and 390-pixel mobile layout.
- Ruff lint and formatting checks and JavaScript syntax checks passed.
- Source and wheel builds succeeded. The wheel was installed into a clean environment and ran a campaign, rendered a compressed report, and replayed the reduced failure outside the source tree, confirming the new CLI commands, bundled fixtures, and report assets. Source and wheel archives were checked for private files.

CI is configured for Python 3.10, 3.11, 3.12, and 3.13, installed-wheel execution outside the source tree, and Chromium. The repository's Actions results are the authority for whether those checks have passed on GitHub.

## Scripted reference run

The default 24 fixtures × 3 modes execute 72 episodes. With one trial and a 16-step limit, the deterministic scripted policies produce:

| Mode | Correctly reported completions | Duplicate-effect cases | Unknown outcomes | No committed effect |
|---|---:|---:|---:|---:|
| Plain | 6 / 24 | 15 | 1 | 3 |
| Guided | 9 / 24 | 11 | 1 | 4 |
| Guarded | 14 / 24 | 1 | 7 | 5 |

These are reference mechanism demonstrations, **not AI model results**. The guarded duplicate is provider redelivery without idempotency support. The increased unresolved/missing outcomes show the cost of refusing unsafe retries. Legitimate authorization rejections count as task noncompletion.

## Published v0.2 evidence

The [failure study](failure-study.md) reports the complete 64-case seeded campaign (192 episodes), 24 real-HTTP cases, five-to-two-action reduction, and the preserved before/after regression. Machine-readable artifacts and their SHA-256 hashes are under [evidence](evidence/manifest.json). The default named-suite unknown count increased to seven after the fix because a rejected in-flight retry no longer resolves earlier uncertainty.

## Not validated by this release

- No live OpenAI or Anthropic model run, comparative model ranking, or model cost estimate.
- No creation or closure of a real GitHub issue through the adapter.
- No production deployment, multi-host recovery, power-loss testing, disk-corruption testing, or distributed clock guarantee.
- No claim of a representative benchmark, novel recovery algorithm, universal exactly-once execution, or a security sandbox.

See methodology.md and architecture.md for the assumptions that constrain results.
