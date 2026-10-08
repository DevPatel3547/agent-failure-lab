# Connector guide

## Minimal Python interface

A connector exposes a `Contract`, `clock()`, `create(operation_id, title, key)`, `lookup(operation_id)`, and `wait()`. Each operation returns an `Observation`.

The gateway assumes `rate_limited` means **no write was accepted**, and `rejected` means a definite rejection of that request. Timeouts, malformed acknowledgements, server errors, and ambiguous responses must remain `uncertain`. Do not claim these stronger semantics unless the underlying API provides them.

Lookup returning nothing remains inconclusive. A connector must not report `found` just because it can manufacture an id; it must reconcile with provider state. Keys must cover the whole operation and identical payload, including its durable dispatch behavior.

## Reference HTTP protocol: afl-ticket-v1

Run `afl fixture-server --scenario lost-ack`. This binds only to `127.0.0.1` and supports:

| Method | Path | Purpose |
|---|---|---|
| GET | `/capabilities` | Protocol name and public contract |
| GET | `/clock` | Public logical clock |
| POST | `/tickets` | `operation_id`, `title`, nullable `idempotency_key` |
| GET | `/tickets?operation_id=...` | Reconcile visible tickets |
| POST | `/clock/advance` | Wait one logical tick; send `{}` |

The server never exposes the effect ledger or fault fixture through HTTP. It is an unauthenticated **local testing service**, not a production server. `examples/http_recovery.py` uses the public protocol through `HTTPConnector` and the durable gateway. A fresh namespace and fresh gateway database produce a fresh demonstration. The two databases intentionally represent different persistence boundaries.

HTTPS is required for non-loopback endpoints. The JSON transport has a timeout and a 2 MB response limit. It never follows redirects or retries automatically. Connect only to trusted services: URL checks are not an SSRF defense for an internet-facing application.

## GitHub Issues adapter (opt-in library integration)

Use only a repository you own and intend to use for synthetic issues. Supply a least-privilege local token with the required repository issue access. The adapter reads `GH_TOKEN` or `GITHUB_TOKEN`, or an explicitly passed token; it does not extract credentials from the GitHub CLI.

```python
from agent_failure_lab.connectors import GitHubIssueConnector
from agent_failure_lab.gateway import RecoveryGateway
from agent_failure_lab.storage import Database

connector = GitHubIssueConnector("YOUR_OWNER/YOUR_TEST_REPO", allow_writes=True)
gateway = RecoveryGateway(
    connector,
    Database("runs/github.sqlite3"),
    operation_id="my-unique-test-operation",
    title="Synthetic recovery test",
)
result = gateway.create("Synthetic recovery test")
print(result.to_dict())
# If uncertain, gateway.create() reconciles and blocks another ambiguous unkeyed write.
# Reuse this operation id and database when recovering the same task.
# Close only the test issue you have positively identified:
# connector.close(result.ticket_id)
```

Without `allow_writes=True`, creation is refused before a network request. Test issues carry a visible test prefix and hashed operation marker. Lookup scans at most 1,000 recent issues, including closed issues and excluding pull requests. Marker absence is never definitive. GitHub issue creation is treated as non-idempotent; uncertainty can therefore require manual reconciliation. The adapter does not automatically close issues or classify a real repository's authoritative effects.

This release tests GitHub request construction, ambiguous responses, pagination behavior, and marker reconciliation with mocked transport. It does not claim a live GitHub fault experiment.

## Model API references

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling): Responses API function tools, strict schemas, one tool call per turn.
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create): tool definitions and `tool_use` blocks.
- [GitHub REST Issues](https://docs.github.com/en/rest/issues/issues): create, list, and update issue endpoints.

Provider requests send the synthetic task, contract, and public history. Keep real credentials and private ticket contents out of fixtures and prompts.
