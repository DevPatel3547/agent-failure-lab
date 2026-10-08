"""Bounded JSON HTTP transport with no automatic retries or redirects."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse


class TransportError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Credentials and fragments are not allowed in endpoint URLs")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and local)):
        raise ValueError("Endpoints must use HTTPS, or HTTP on loopback for local tests")


class JSONTransport:
    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, url: str, payload: dict | None = None, headers: dict | None = None):
        validate_url(url)
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "agent-failure-lab/0.1.0",
                **(headers or {}),
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise TransportError("Response exceeds the 2 MB limit")
            return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never include raw error bodies or request headers in saved traces.
            status = exc.code
            exc.close()
            raise TransportError(f"HTTP {status}; request was not automatically retried", status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError(
                "Connection failed or timed out; request was not automatically retried"
            ) from None
        except (UnicodeError, json.JSONDecodeError):
            raise TransportError("Provider returned malformed JSON") from None
