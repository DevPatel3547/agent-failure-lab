# Contributing

A useful contribution starts with one reproducible connector failure. Describe the observed tool response, the possible committed effect, and what evidence would resolve the ambiguity. Remove private data and credentials.

1. Add a small scenario or connector behavior with explicit semantics.
2. Add a test that fails for the behavior being fixed and passes for the fix.
3. Keep the model's public context separate from the simulator's hidden world.
4. Report availability costs as well as duplicate prevention.
5. Run `python3 -m unittest discover -s tests -v`, `ruff check .`, and `ruff format --check .`.
6. For report changes, run `python3 tests/browser_check.py` after installing the development extra and Playwright Chromium.

Do not add fabricated live-model results or claims of universal exactly-once execution. New dependencies should solve a concrete need. Public issues should contain only sanitized reproductions; see SECURITY.md for sensitive reports.
