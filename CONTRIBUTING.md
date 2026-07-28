# Contributing

Thank you for helping improve `reason-assembly`.

## Development setup

```sh
git clone https://github.com/jroth1111/reason-assembly.git
cd reason-assembly
uv sync --locked --dev
uv run pytest -q
```

Use Python 3.11 or newer; CI covers 3.11 through 3.13. Keep changes focused and add tests for behavior changes.
Before opening a pull request, run:

```sh
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q scripts tests
```

## Pull requests

Explain the user-visible behavior, safety implications, and verification
performed. Preserve the distinctions between catalogued, eligible, and healthy
models. Catalogue absence—not transient health—controls smart-alias pruning.

Never include real proxy configuration, authentication material, private model
responses, run artifacts, personal information, local paths, or provider
credentials in a commit, fixture, issue, or pull request. Use synthetic
`example.invalid` data in tests.

Public changes should update the README, protocol, and skill contract together
when they change a documented flow.
