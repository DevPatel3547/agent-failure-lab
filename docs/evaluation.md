# Auditable model evaluation

**Status: the machinery is tested; no live-model results have been published.** API protocol doubles test infrastructure, not model behavior. The SDK demonstration also uses scripted inference.

`afl evaluate` adds a frozen experiment plan and durable request accounting to the existing single-run adapter. Plain, guided, and guarded use the same provider/model, scenarios, trials, and step budget. The primary outcome is correctly reported task completion. Duplicate effects, false claims, missing effects, and unknown outcomes remain separate secondary outcomes. The service is simulated, even when decisions come from a live model.

## Freeze the plan before calling a model

Select an explicit model snapshot available to your account. Look up its **full documented context window**, maximum applicable input/output rates, and any long-context premiums in the provider's official documentation. Do not substitute an expected prompt size for the full context window. `--price-source` records the source; the software does not fetch or verify prices.

```sh
afl eval-plan --provider openai --model YOUR_MODEL_SNAPSHOT \
  --scenario normal --scenario lost-ack --scenario late-no-keys \
  --input-usd-per-million INPUT_RATE --output-usd-per-million OUTPUT_RATE \
  --context-tokens FULL_MODEL_CONTEXT_WINDOW \
  --price-source https://developers.openai.com/api/docs/pricing \
  --max-usd 10 --max-requests 300 --trials 3 --max-steps 12 \
  --output reports/evaluation-plan.json

afl evaluate reports/evaluation-plan.json --dry-run
```

Replace the uppercase values; the command deliberately does not choose prices or a model. Planning and dry runs use no credentials and make no network requests. The plan captures all scenario definitions, the treatment schedule seed, primary/secondary outcomes, budgets, and a SHA-256 fingerprint of the package's Python implementation (including prompts, tools, runner and grader). Changing implementation or plan contents is rejected. Plan files are created exclusively rather than overwritten.

For a public study, review the plan and commit it **before** the first model call. A hash detects changes; it is not proof of independent preregistration or tamper resistance. The plan includes synthetic fixtures only, but review any custom fixtures before sharing them.

## Execute and resume

Configure `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the process environment. Do not put a key in the plan, repository, or report. `.env` is not automatically loaded.

```sh
afl evaluate reports/evaluation-plan.json \
  --db runs/evaluation.sqlite3 --output reports/evaluation

# Same command after an interruption: resumes the remaining frozen schedule.
afl evaluate reports/evaluation-plan.json \
  --db runs/evaluation.sqlite3 --output reports/evaluation
```

No completed episode is rerun. A process-interrupted episode resumes its saved world/history; a model response lost before checkpointing can require a new request, which is counted again. Provider-error episodes remain errors; the next invocation continues with the next case. A terminal budget stop remains terminal. Increasing the budget requires a separately identified experiment rather than rewriting the old result.

An OS lock prevents two runners from sharing one database simultaneously and releases on process exit. The request reservations themselves are atomic SQLite transactions, also tested under concurrent callers. A different plan cannot reuse the same budget database. Deleting the database or using a new database starts a separate budget: this is not an account-wide spending control. Keep the database for all resumes.

The original `afl run` and `afl resume` commands retain their simpler per-invocation request limits. They do **not** inherit this evaluation ledger.

## How the cost bound works

Before every model request, persist a reservation:

```
reservation = full_model_context_tokens × maximum_input_rate
            + max_output_tokens × maximum_output_rate
```

Rates are per token in that formula. Internally, arithmetic uses decimal prices and integer microdollars, rounding reservations up and the spending allowance down. The full-context reservation is intentionally conservative; it does not assume a tokenizer ratio or estimate hidden prompt overhead. No provider retry or built-in paid tool is enabled by the adapter.

After a response with valid usage, replace the reservation with token usage priced at the supplied rates. Missing/malformed usage, transport errors, cancellation, or process death retain the full reservation. Anthropic cache counters are included, without assuming a discount. If reported token counts exceed the declared bound, record the violation and halt further requests; a wrong bound cannot undo an already billed call.

**This is a conditional application budget, not a provider billing guarantee.** It depends on accurate model context limits and rates covering the applicable tier, long-context and caching charges. Prices can change; verify them before execution. The ledger is not an invoice and does not account for unrelated account traffic, taxes, or provider billing adjustments. Model aliases can also change behavior; prefer snapshots and record the exact identifier.

## Read the entire result

The generated HTML exposes the plan, request accounting, and paired outcomes. `results.json` carries all traces and metadata; `evaluation.json` is the compact coverage/accounting summary.

- Coverage lists scheduled, recorded and unstarted episodes, plus counts by execution status. Exit code 2 indicates incomplete evidence; exit code 0 requires the full schedule without execution errors/budget stops.
- The paired table compares plain versus guided and plain versus guarded by scenario and trial: both succeeded, plain only, treatment only, neither. It reports missing/excluded pairs explicitly.
- Step-limited episodes count as observed outcomes; provider errors and budget/interrupted episodes do not silently enter a success-rate denominator as usable model decisions.
- Request records contain payload/response hashes, charged or reserved amount, reported tokens, timing and status. They exclude keys, headers and raw provider bodies. Agent-visible decisions and synthetic tool histories remain in the episode report.
- Injected test transports are explicitly labelled in metadata and the HTML. They cannot be resumed as live responses in the same evaluation.

Paired success differences are descriptive. Trials share scenarios; treating every trial as an independent sampled task would exaggerate statistical precision. The selected fixtures are not a production task distribution. Do not make a general model ranking from a small campaign or select only successful traces.

## Before making a model-performance claim

Publish the original plan, exact model identifier, complete report, request ledger, all failed/unfinished cells, and a short interpretation that includes negative results. Run a separate deterministic reference baseline on the same scenario set. Inspect model failures before changing prompts, then freeze a **new** plan for the next experiment. A runtime integration test and a model comparison answer different questions.
