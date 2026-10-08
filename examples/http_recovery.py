"""Run after: python -m agent_failure_lab fixture-server --scenario lost-ack."""

from agent_failure_lab.connectors import HTTPConnector
from agent_failure_lab.gateway import RecoveryGateway
from agent_failure_lab.storage import Database

connector = HTTPConnector("http://127.0.0.1:8769")
gateway = RecoveryGateway(
    connector, Database("runs/http-client.sqlite3"), "http-example-1", "Synthetic example"
)
print("First call:", gateway.create("Synthetic example").to_dict())
print("Recovery:", gateway.create("Synthetic example").to_dict())
