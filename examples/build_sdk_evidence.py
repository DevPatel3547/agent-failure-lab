"""Rebuild the SDK-only synthetic dataset, without altering historical v0.2 evidence."""

import gzip
import hashlib
import json
from pathlib import Path

from agent_failure_lab.agents_integration import run_sdk_demo
from agent_failure_lab.report import write_report


def publish(data, root):
    evidence = root / "docs" / "evidence"
    target = evidence / "v0.3-sdk.json.gz"
    target.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    manifest = {
        "package_version": data["experiment"]["version"],
        "live_models": False,
        "integration": data["experiment"]["integration"],
        "integration_version": data["experiment"]["integration_version"],
        "openai_client_version": data["experiment"]["openai_client_version"],
        "episodes": len(data["results"]),
        "sha256": {target.name: hashlib.sha256(target.read_bytes()).hexdigest()},
        "note": "Real SDK execution and local HTTP faults. Scripted inference, synthetic service. "
        "Separate from the historical v0.2 manifest; hashes cover compressed bytes.",
    }
    (evidence / "v0.3-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rendered = write_report(data, root / "reports" / "sdk")
    (root / "docs" / "sdk.html").write_bytes(rendered.read_bytes())
    print(json.dumps({"manifest": manifest, "summary": data["summary"]}, indent=2))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    publish(run_sdk_demo(root / "reports" / "sdk"), root)
