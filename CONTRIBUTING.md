# Contributing

Thanks for helping improve Wholesale Order Parser.

## Before opening a change

1. Open an issue for user-visible behavior changes or security-sensitive work.
2. Keep extraction and matching advisory: no change may bypass explicit human
   review, approval provenance, or immutable approved snapshots.
3. Never include customer orders, credentials, databases, logs, exports, or
   provider responses containing private data.

## Local checks

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
ruff check app tests serve_demo.py
pytest --ignore=tests/test_live_gemini.py
pip-audit -r requirements.txt
```

The live Gemini test is opt-in because it uses a real API key and quota.

## Pull requests

- Keep each pull request focused and explain the user or operator impact.
- Add regression tests for fixes and security controls.
- Update `README.md`, `SECURITY.md`, or `DEPLOY.md` when behavior or operating
  assumptions change.
- Wait for CI and CodeQL to pass before merging.

For vulnerabilities, do not open a public issue. Follow `SECURITY.md` and use
GitHub's private vulnerability reporting.
