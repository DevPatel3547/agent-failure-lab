# Validation record

This release separates an implemented feature from a live-service claim.

## Checked locally

- All 48 backend tests passed on Python 3.12, including real loopback HTTP requests, subprocess termination after commit, concurrent recovery/backoff, all-fixture replay, provider protocol validation, incomplete-run accounting, GitHub pagination, and report escaping. The original 43-test suite also passed on Python 3.13.
- Chromium acceptance checks passed for all 72 displayed cases, strategy/search/outcome filters, explicit world-state reveal, trace scrubbing, JSON download, injected HTML displayed as text, zero external report requests, and 390-pixel mobile layout.
- Ruff lint and formatting checks and JavaScript syntax checks passed.
- Source and wheel builds succeeded. The wheel was installed into a clean environment and ran a lost-acknowledgement demo outside the source tree, confirming the CLI, bundled fixtures, and report assets. Source and wheel archives were checked for private files.

CI is configured for Python 3.10, 3.11, 3.12, and 3.13, installed-wheel execution outside the source tree, and Chromium. The repository's Actions results are the authority for whether those checks have passed on GitHub.

## Scripted reference run

The default 24 fixtures × 3 modes execute 72 episodes. With one trial and a 16-step limit, the deterministic scripted policies produce:

| Mode | Correctly reported completions | Duplicate-effect cases | Unknown outcomes | No committed effect |
|---|---:|---:|---:|---:|
| Plain | 6 / 24 | 15 | 1 | 3 |
| Guided | 9 / 24 | 11 | 1 | 4 |
| Guarded | 14 / 24 | 1 | 6 | 5 |

These are reference mechanism demonstrations, **not AI model results**. The guarded duplicate is provider redelivery without idempotency support. The increased unresolved/missing outcomes show the cost of refusing unsafe retries. Legitimate authorization rejections count as task noncompletion.

## Not validated by this release

- No live OpenAI or Anthropic model run, comparative model ranking, or model cost estimate.
- No creation or closure of a real GitHub issue through the adapter.
- No production deployment, multi-host recovery, power-loss testing, disk-corruption testing, or distributed clock guarantee.
- No claim of a representative benchmark, novel recovery algorithm, universal exactly-once execution, or a security sandbox.

See methodology.md and architecture.md for the assumptions that constrain results.
