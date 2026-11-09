# CodexPic Guard maintenance

Canonical repository: https://github.com/zaofenMachine/CodexPic-Guard

- Keep source changes, tests, documentation and release metadata in this repository. Do not import the parent workspace's private audits or third-party research checkout.
- Never commit sessions, screenshots, image caches, credentials, local settings, `.test-data/`, or raw `reports/`. Public measurements belong in `docs/validation-summary.json` after review.
- Preserve explicit activation and reversible configuration changes. Tests use temporary data directories and isolated Codex homes; do not enable routing, restart the user's Codex, or change system/ClaudeSafe proxy rules as part of repository maintenance.
- Preserve loopback-only serving, scoped original retrieval, integrity checks, protected-image errors and no silent DIRECT fallback. Do not bypass failed checks by forwarding the original oversized request.
- Distinguish request bytes from VPN billing, cached/uncached tokens, model cost and visual accuracy. The 95.4% result is a historical offline replay, not a production savings guarantee.
- Read `docs/ROADMAP.md` before changing image retention or transport. Stable context and native incremental transport remain unresolved research areas.
- Run `python -m unittest discover -s tests -v` for application changes, and `python scripts/build_release.py` for releases. `tests.native_codex_smoke` is an optional isolated local mock integration; `scripts/live_smoke.py` is an explicit opt-in and must not run in CI.
- Update `pyproject.toml`, `src/codexpic_guard/__init__.py`, `CHANGELOG.md` and validation notes together when changing versions. Verify the remote commit/tag after publishing; do not force-push published history.
