# Contributing to K7

Thanks for your interest in contributing.

K7 stays lean on purpose:

- Infra is a single Ansible playbook (`src/k7/deploy/`)
- CLI (Typer) and API (FastAPI) both wrap the same `core` module
- Tooling: **uv**, **ruff**, **ty**, **pytest**

## Project direction

See [`ROADMAP.md`](ROADMAP.md) for priorities. Near-term focus is the
public release pipeline (PPA / GHCR / PyPI) after `Katakate/k7d` is
published.

## Repo layout

- `src/k7/` — CLI, core, API, Ansible playbook
- `src/k7_sdk/` — Python SDK (PyPI: **`k7-sdk`**)
- `src/katakate/` — deprecated import shim → `k7_sdk`
- `tests/` — unit + integration
- `utils/` — helper scripts
- `docs/BACKENDS.md` — backend comparison (full docs: https://docs.katakate.org)

## Packaging

- Root packaging (`setup.py`) builds the **`k7-sdk`** wheel for PyPI.
- CLI / playbook assets ship via the Debian package / install path, not
  the PyPI SDK package.

## Code style

- Python: PEP 8, explicit types on public APIs, early returns
- Lint / format with Ruff via uv:

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
# or: make lint
```

Typecheck: `make typecheck`. Unit tests: `make test`.

## Fast CLI iteration (`dev.sh`)

Avoid `make build && make install` while hacking the CLI:

```bash
./src/k7/cli/dev.sh --help
./src/k7/cli/dev.sh list
./src/k7/cli/dev.sh create --name test --image alpine:latest
```

Same flags as the installed `k7` binary. For playbook / core / API
changes, escalate: `dev.sh` → `make test-integration-remote` →
`make build && make install` on a Linux node. Stack targets Linux x86;
do not deploy or run the full stack on macOS ARM.

## Releases

- Keep versions aligned in `src/k7/__init__.py`, `src/k7_sdk/__init__.py`,
  `setup.py`, `pyproject.toml`, and `debian/changelog`
- Tag `vX.Y.Z` once the public release pipeline is live
- See [`CHANGELOG.md`](CHANGELOG.md)

## Reporting issues

Include steps, expected vs actual, logs, and environment (OS, arch,
backend: `kfd` / `kql` / `k7d`, single- vs multi-node). Security reports:
see [`SECURITY.md`](SECURITY.md).
