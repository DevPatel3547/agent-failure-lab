"""Reference HTTP and opt-in GitHub issue connectors. No network calls at import."""

from __future__ import annotations

import json
import math
import os
import re
import time
from urllib.parse import quote, urlencode

from .schema import Contract, Observation, digest
from .transport import JSONTransport, TransportError, validate_url


class HTTPConnector:
    """Connect to the bundled fixture server or another implementation of its contract."""

    def __init__(self, base_url: str, transport=None):
        validate_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.transport = transport or JSONTransport()
        data = self.transport.request("GET", self.base_url + "/capabilities")
        if not isinstance(data, dict) or data.get("protocol") != "afl-ticket-v1":
            raise ValueError("Endpoint does not advertise the afl-ticket-v1 protocol")
        self.contract = Contract(**data["contract"])

    def clock(self) -> float:
        value = float(self.transport.request("GET", self.base_url + "/clock")["clock"])
        if not math.isfinite(value) or value < 0:
            raise ValueError("Connector returned an invalid clock")
        return value

    def create(self, operation_id: str, title: str, key: str | None = None) -> Observation:
        try:
            result = self.transport.request(
                "POST",
                self.base_url + "/tickets",
                {"operation_id": operation_id, "title": title, "idempotency_key": key},
            )
            return Observation.from_dict(result)
        except (TransportError, ValueError, TypeError):
            return Observation(
                "uncertain", "HTTP response unavailable or malformed; commit status is unknown"
            )

    def lookup(self, operation_id: str) -> Observation:
        try:
            result = self.transport.request(
                "GET", self.base_url + "/tickets?" + urlencode({"operation_id": operation_id})
            )
            return Observation.from_dict(result)
        except (TransportError, ValueError, TypeError):
            return Observation("unavailable", "HTTP lookup unavailable")

    def wait(self) -> Observation:
        return Observation.from_dict(self.transport.request("POST", self.base_url + "/clock/advance", {}))


class GitHubIssueConnector:
    """Create labelled test issues; no native idempotency contract is assumed.

    A marker permits best-effort reconciliation. A missing marker is NOT proof
    that an earlier create failed. RecoveryGateway therefore blocks uncertain
    unkeyed retries. Every write requires explicit construction with allow_writes.
    """

    def __init__(self, repository: str, allow_writes: bool = False, token: str | None = None, transport=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Repository must be owner/name")
        self.repository = repository
        self.allow_writes = allow_writes
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("Set GH_TOKEN or GITHUB_TOKEN locally for the GitHub connector")
        self.transport = transport or JSONTransport()
        self.contract = Contract(False, True, clock_unit="seconds")
        self.base_url = "https://api.github.com/repos/" + repository

    def _request(self, method: str, path: str, body=None):
        return self.transport.request(
            method,
            self.base_url + path,
            body,
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    @staticmethod
    def marker(operation_id: str) -> str:
        return "<!-- agent-failure-lab:" + digest(operation_id) + " -->"

    def clock(self) -> float:
        return time.time()

    def create(self, operation_id: str, title: str, key: str | None = None) -> Observation:
        if not self.allow_writes:
            return Observation(
                "rejected", "GitHub writes are disabled; use an owned test repository and allow_writes=True"
            )
        if key is not None:
            return Observation("rejected", "GitHub issue creation offers no idempotency contract here")
        body = {
            "title": "[Agent Failure Lab test] " + title,
            "body": "Synthetic connector test. This issue can be closed after validation.\n\n"
            + self.marker(operation_id),
        }
        try:
            data = self._request("POST", "/issues", body)
            number = data.get("number") if isinstance(data, dict) else None
            if type(number) is not int:
                return Observation("uncertain", "Issue response has no valid number; do not retry blindly")
            return Observation("created", "GitHub acknowledged issue creation", str(number))
        except TransportError as exc:
            if exc.status in {400, 401, 404, 422}:
                return Observation("rejected", f"GitHub rejected the create request (HTTP {exc.status})")
            return Observation(
                "uncertain", "GitHub did not confirm the outcome; reconcile before any further write"
            )

    def lookup(self, operation_id: str) -> Observation:
        marker = self.marker(operation_id)
        ids = []
        try:
            # Bounded scan; absence is never definitive, even when every page is read.
            for page in range(1, 11):
                data = self._request(
                    "GET", f"/issues?state=all&sort=created&direction=desc&per_page=100&page={page}"
                )
                if not isinstance(data, list):
                    return Observation("unavailable", "Malformed GitHub lookup response")
                for item in data:
                    if (
                        isinstance(item, dict)
                        and "pull_request" not in item
                        and isinstance(item.get("body"), str)
                        and marker in item["body"]
                        and type(item.get("number")) is int
                    ):
                        ids.append(str(item["number"]))
                if len(data) < 100:
                    break
        except TransportError:
            return Observation("unavailable", "GitHub lookup failed; absence was not established")
        if ids:
            return Observation("found", "Matching test issues found", ids[0], tuple(ids))
        return Observation(
            "not_found", "No marker found in the bounded scan; this does not establish absence"
        )

    def wait(self) -> Observation:
        time.sleep(1)
        return Observation("waited", "One second elapsed")

    def close(self, ticket_id: str) -> None:
        if not self.allow_writes or not ticket_id.isdigit():
            raise ValueError("Closing a test issue requires allowed writes and a numeric issue id")
        self._request("PATCH", "/issues/" + quote(ticket_id, safe=""), {"state": "closed"})


def fixture_handler(connector):
    """Return a loopback fixture server handler; never expose private grader state."""
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_json(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            from dataclasses import asdict

            route = urlparse(self.path)
            if route.path == "/capabilities":
                self.send_json({"protocol": "afl-ticket-v1", "contract": asdict(connector.contract)})
            elif route.path == "/clock":
                self.send_json({"clock": connector.clock()})
            elif route.path == "/tickets":
                operation = parse_qs(route.query).get("operation_id", [""])[0]
                if not operation or len(operation) > 200:
                    self.send_json({"error": "Invalid operation id"}, 400)
                else:
                    self.send_json(connector.lookup(operation).to_dict())
            else:
                self.send_json({"error": "Not found"}, 404)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_384:
                    raise ValueError("Invalid body size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Object required")
                if self.path == "/tickets":
                    from .schema import Action

                    operation = data["operation_id"]
                    if not isinstance(operation, str) or not 1 <= len(operation) <= 200:
                        raise ValueError("Invalid operation id")
                    action = Action.parse(
                        "create_ticket",
                        {"title": data["title"], "idempotency_key": data.get("idempotency_key")},
                    )
                    self.send_json(
                        connector.create(
                            operation, action.arguments["title"], action.arguments["idempotency_key"]
                        ).to_dict()
                    )
                elif self.path == "/clock/advance":
                    self.send_json(connector.wait().to_dict())
                else:
                    self.send_json({"error": "Not found"}, 404)
            except (ValueError, TypeError, KeyError):
                self.send_json({"error": "Invalid request"}, 400)

    return Handler
