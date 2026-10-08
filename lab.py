"""Compatibility entry point; prefer python -m agent_failure_lab."""

from agent_failure_lab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
