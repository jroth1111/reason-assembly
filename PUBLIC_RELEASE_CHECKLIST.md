# Public release checklist

Use this checklist before publishing a fork or release.

## Build from an allowlist

Copy only source, tests, public documentation, the lockfile, and intended
examples into a clean repository. Do not publish a long-lived local working
directory whose history or ignored files have not been audited.

## Omit machine-local and generated files

- virtual environments, bytecode, test/lint/type caches, coverage, and builds;
- `.env` files, proxy configuration, local YAML overrides, auth directories,
  credentials, provider keys, certificates, cookies, and session material;
- council state, run artifacts, sync receipts, evidence, patches, logs, and
  temporary files;
- editor, operating-system, and local agent scratch state.

Confirm exclusions with:

```sh
git status --short --ignored
git check-ignore -v .venv config.yaml runs/example.json
```

## Remove personal and sensitive information

Inspect both tracked content and Git history for:

- real names, personal email addresses, usernames, account or tenant IDs;
- absolute home-directory paths, device names, host names, IP addresses, and
  private network URLs;
- API keys, bearer tokens, passwords, cookies, authorization headers, proxy
  credentials, and credential-bearing URLs;
- private prompts, source documents, provider responses, run evidence, and
  repository names that are not already public.

Use synthetic domains such as `example.invalid` and obviously synthetic
credential values in fixtures. Do not print a suspected secret merely to prove
that a scanner found it.

## Verify the release

```sh
uv sync --locked --dev
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src scripts tests
git diff --check
```

Then inspect:

```sh
git ls-files
git log --all --oneline --decorate
git rev-list --objects --all
```

After publishing, clone the repository without credentials into a new temporary
directory and repeat the tracked-file and sensitive-data checks. Confirm the
repository is public, the default branch is correct, and CI succeeds.
