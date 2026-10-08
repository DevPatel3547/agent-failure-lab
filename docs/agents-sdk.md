# OpenAI Agents SDK integration

This integration runs the actual `openai-agents==0.23.1` runtime, function-tool schema handling, tool execution, and turn loop against the lab's HTTP fault proxy and a separate local service process. It uses `openai==3.26.1` types. **Model inference is a deterministic test double. No model API calls or remote tracing are used.**

```sh
python3 -m pip install -e '.[agents]'
python3 -m agent_failure_lab sdk-demo
python3 -m agent_failure_lab serve --directory reports/sdk
```

The optional extra is not required by the base package. The official SDK's [custom Model interface](https://openai.github.io/openai-agents-python/ref/models/interface/) provides the test seam; the integration does not reimplement its Runner.

## What the measured run shows

Four HTTP faults × two provider capabilities × two tool implementations produce 16 cases. The faults are disconnect-before-forwarding, dropped response, corrupted JSON and pre-forward 503. The same deliberately simple retry policy runs through both implementations.

| Tool implementation | Correct completions | Duplicate cases | Unknown / no effect |
|---|---:|---:|---:|
| Direct unkeyed call | 4 / 8 | 4 | 0 |
| Durable recovery gateway | 6 / 8 | 0 | 2 |

The unkeyed gateway cannot safely retry after an ambiguous pre-forward disconnect/503: the client cannot establish that no write reached the service. Those two tasks remain unresolved. The wire trace supplies evidence to the grader; it is not revealed to the caller to make recovery artificially easier.

These are **integration results**, not model results, an SDK vulnerability, an SDK endorsement, or third-party adoption. The direct tool intentionally omits recovery, and the model double intentionally retries. The stronger reference policy in the [HTTP study](failure-study.md) already matches the gateway's effect outcomes. The purpose here is to prove that the boundary can be used inside a separately maintained runtime.

The complete [portable dataset](evidence/v0.3-sdk.json.gz) includes wire events, committed effects, model-visible SDK inputs and package versions. [View the SDK report](https://devpatel3547.github.io/agent-failure-lab/sdk.html). Its independent manifest is [here](evidence/v0.3-manifest.json).

## Use the tools in your own agent

`agent_failure_lab.agents_integration.ticket_tools` returns actual SDK `FunctionTool` objects. Bind them to an application-owned logical operation:

```python
from agents import Agent
from agent_failure_lab.agents_integration import ticket_tools
from agent_failure_lab.connectors import HTTPConnector
from agent_failure_lab.storage import Database

connector = HTTPConnector("http://127.0.0.1:8770")
agent = Agent(
    name="Ticket worker",
    model=your_configured_model,
    tools=ticket_tools(
        connector,
        Database("runs/worker.sqlite3"),
        operation_id="your-durable-business-operation-id",
        title="The authorized task title",
    ),
)
```

Use a new operation ID for a new task; retain the same ID and SQLite journal across retries of the same task. SDK tool-call IDs identify invocations, not the business operation: two distinct call IDs may request the same ticket. The SDK acceptance check deliberately issues parallel calls with distinct call IDs and verifies that guarded recovery creates one effect for both keyed and unkeyed services. The tool functions offload synchronous I/O so they do not block the SDK event loop.

The title is bound when the gateway is constructed; a changed payload is rejected. This is not an authorization system for arbitrary tasks. Multi-host coordination, real service clock mapping, unsupported API contracts, and session persistence still require application design. The demo does not assert that resuming a complete SDK session is implemented by the lab.

## Run the acceptance check

```sh
python3 tests/sdk_check.py
```

It runs all 16 cases plus two parallel-call cases, checks effects, and rejects external socket connections in the parent process. Tracing is explicitly disabled. CI installs the pinned optional SDK on Python 3.12 and runs this check before the browser checks. The core package test matrix remains dependency-free at runtime.
