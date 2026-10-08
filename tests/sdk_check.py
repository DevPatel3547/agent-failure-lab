"""Acceptance test with the actual optional SDK; no provider calls are allowed."""

from pathlib import Path
import socket
from unittest.mock import patch

from agent_failure_lab.agents_integration import run_sdk_case, run_sdk_demo


def main():
    external = []
    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            external.append(address[0])
            raise AssertionError("SDK demo attempted an external connection")
        return connect(sock, address)

    with patch.object(socket.socket, "connect", local_only):
        data = run_sdk_demo(Path("reports/sdk"))
        summaries = {row["mode"]: row for row in data["summary"]}
        assert len(data["results"]) == 16
        assert summaries["plain"]["duplicates"] == 4
        assert summaries["guarded"]["duplicates"] == 0
        assert summaries["guarded"]["task_success"] == 6
        assert summaries["guarded"]["unknown"] == 2
        assert all(row["sdk_model_inputs"] for row in data["results"])
        for keys in (True, False):
            guarded = run_sdk_case(
                Path("reports/sdk/private-runs"), "drop_response", keys, "guarded", parallel_first=True
            )
            assert guarded["metrics"]["effects"] == 1, guarded["metrics"]
            assert guarded["metrics"]["duplicates"] == 0
            # Two different SDK call IDs for the same logical business operation.
            calls = [
                item
                for history in guarded["sdk_model_inputs"]
                for item in history["input"]
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            assert len({call["call_id"] for call in calls}) >= 2
    assert not external, external
    print("SDK checks passed: 16 HTTP cases, two parallel-call cases, no external connections.")


if __name__ == "__main__":
    main()
