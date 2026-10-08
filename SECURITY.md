# Security and data boundaries

This is a local evaluation workbench, not a secure execution sandbox or production write gateway. Run trusted Python code and fixtures. Local SQLite files and reports contain full investigation data, including hidden world state.

Keep API keys in your environment or secret manager. Do not commit `.env` files, run databases, or private incident data. The project performs best-effort credential redaction; it cannot guarantee removal of all secrets or personal data. Inspect exported reports before sharing them.

The fixture and report servers are loopback-only development servers with no authentication. Do not expose them publicly. The HTTP connector accepts trusted HTTPS endpoints; it is not an SSRF boundary for an internet-facing API. Live model calls send the task and public history to the selected provider.

The GitHub adapter is disabled for writes by default. Use an owned test repository and minimal permissions when opting in. A local journal does not provide a universal exactly-once guarantee, and an ambiguous operation may need manual reconciliation.

For a sensitive vulnerability, use GitHub private vulnerability reporting if enabled on this repository. Do not post credentials, private traces, or exploitable sensitive details in a public issue. If private reporting is unavailable, open a minimal issue asking for a private reporting channel without including the sensitive details.

The v0.2 fault proxy is also loopback-only with a fixed loopback upstream. It intentionally disrupts requests; use an owned local test service and synthetic data. Paths can contain sensitive identifiers even though headers, queries, and bodies are omitted from its trace. It is not a production reverse proxy, TLS interceptor, or safe boundary for arbitrary untrusted traffic.
