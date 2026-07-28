# ccycouncil

`ccycouncil` is an evidence-backed, multi-model council for consequential
decisions, adversarial review, code review, and competing implementation
workflows. It runs explicitly against a local
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)-compatible proxy,
discovers the models that proxy currently advertises, assigns models to roles,
and records why a result was or was not allowed to become final.

The council is deliberately **model-ID-only**. Friendly proxy aliases such as
`worker` remain a launcher concern; the council stores and reports the actual
catalogued model IDs it used.

> [!IMPORTANT]
> This is orchestration software, not a truth oracle. Council traffic leaves
> your machine for whichever providers your proxy is configured to use. Never
> put credentials, secrets, private keys, or unreviewed sensitive data into a
> prompt, context file, source URL, run artifact, issue, or bug report.

## What problem it solves

A majority vote among several language models can still be confidently wrong.
`ccycouncil` instead treats a decision as an evidence process:

- lock the task contract and rubric before seeing candidate answers;
- use independent model roles and typed claims rather than one long group chat;
- distinguish model availability from model suitability and live health;
- request deterministic or independently verified evidence for important claims;
- surface dissent, blockers, uncertainty, and co-failure risk;
- permit a final result only when its finality policy is satisfied; and
- preserve private, replayable artifacts for later review and calibration.

```mermaid
flowchart TD
    A[Task contract] --> B[Lock rubric and risk]
    B --> C[Sync proxy catalogue]
    C --> D[Select eligible model IDs]
    D --> E[Independent proposals]
    E --> F[Evidence and verification]
    F --> G[Red-team and minority checks]
    G --> H[Mirrored judging and aggregation]
    H --> I{Finality gate}
    I -->|evidence supports action| J[semantic_commit]
    I -->|decision supported, no code action| K[verdict_commit]
    I -->|insufficient or conflicting evidence| L[abort]
```

## Availability has three different meanings

These terms are intentionally not interchangeable:

| State | Meaning | What it controls |
| --- | --- | --- |
| **Catalogued** | The model ID is present in the proxy's raw `/v1/models` response. | Alias membership and whether the model can be selected at all. |
| **Eligible** | Matching capability metadata permits the model to perform a requested council role. | Council routing. A raw-only model is retained as `listed-only`, not silently treated as eligible. |
| **Healthy** | A bounded live `doctor` probe reached one terminal health classification. | Diagnostics and operator judgment. Temporary unhealthiness does not remove an advertised alias candidate. |

## Continuous catalogue synchronization

Every council catalogue read performs synchronization, and a proxy launcher can
invoke the same audit before resolving aliases:

```mermaid
flowchart LR
    A[GET /v1/models] --> C{ID sets equal?}
    B[GET capability metadata] --> C
    C -->|yes| D[Attach matching metadata]
    C -->|no| E[Retry once]
    E -->|now equal| D
    E -->|still different| F[Warn; raw IDs remain authoritative]
    F --> G[Matching metadata only]
    G --> H[Raw-only IDs become listed-only]
    D --> I[Prune absent smart-alias candidates]
    H --> I
    I --> J[Write private credential-free receipt]
```

Pruning is membership-based, not health-based. The synchronizer:

- exclusively locks the proxy configuration;
- removes only smart-alias candidates absent from the raw catalogue;
- preserves unrelated YAML text, comments, ownership, and private permissions;
- uses an atomic replacement and never creates credential-bearing backups;
- warns and continues if metadata, pruning, or receipt persistence fails; and
- writes schema-v4, credential-free receipts below
  `~/.local/state/ccycouncil/v4/`.

When metadata remains out of step after one retry, `/v1/models` is authoritative.
Metadata-only entries are excluded. Raw-only entries remain visible as
`listed-only`, which prevents an absent or unqualified model from being selected
while preserving an honest view of the proxy catalogue.

## Requirements

- Python 3.11 or 3.12
- [`uv`](https://docs.astral.sh/uv/)
- a POSIX shell (`zsh` is preferred; `/bin/sh` is the fallback)
- a running CLIProxyAPI-compatible endpoint
- a proxy configuration containing `host`, `port`, and any required
  authentication plus optional `smart-aliases`
- Git for `review` and `implement` workflows

The default proxy configuration path on macOS is:

```text
~/Library/Application Support/AIUsage/CLIProxyAPI/config.yaml
```

Override it on any platform:

```sh
export CCYPROXY_CONFIG=/path/to/config.yaml
```

## Install

Clone the repository and install its locked dependencies:

```sh
git clone https://github.com/jroth1111/ccycouncil.git
cd ccycouncil
uv sync --locked --dev
./bin/ccycouncil --version
```

To make the command available from any directory, add the repository's `bin`
directory to `PATH`, or symlink `bin/ccycouncil` into a directory already on
`PATH`.

## Start with the proxy audit

```sh
ccycouncil sync
ccycouncil sync --json
ccycouncil models
ccycouncil doctor --all-models
ccycouncil doctor --all-models --live --json
```

`sync --json` reports raw, metadata, and council counts and equality; alias
resolution; removed candidates; warnings; and the final outcome. It does not
print proxy credentials. `doctor --all-models --json` adds synchronization and
alias diagnostics and gives every current model one terminal classification.

The public helper in [`examples/ccyproxy-sync.zsh`](examples/ccyproxy-sync.zsh)
shows how a shell launcher can refresh immediately before route resolution.
Launchers should still intersect every resolved candidate with the current raw
catalogue before selecting it.

## Decision and red-team workflows

Provide a prompt directly or from a file:

```sh
ccycouncil decide "Choose a migration strategy for this service"

ccycouncil decide \
  --prompt-file task.md \
  --context architecture.md \
  --source https://example.invalid/design \
  --verify-command "pytest -q" \
  --budget standard \
  --json

ccycouncil red-team \
  "Find the strongest failure modes in this launch plan" \
  --budget quick
```

Budgets are `adaptive`, `quick`, `standard`, and `max`. Their normal call caps
are 12, 30, and 60 for the named fixed budgets; `--max-calls` is authoritative
when supplied. Add explicit route overrides with:

```sh
ccycouncil decide "..." --route proposer=model-id:high
```

Use `--source` only for HTTPS sources. `--verify-command` runs a command you
explicitly authorize; review it with the same care as any other shell command.

## Code review

`review` operates on an explicit Git target:

```sh
ccycouncil review --repo /path/to/repo --working-tree
ccycouncil review --repo /path/to/repo --staged
ccycouncil review --repo /path/to/repo --base main
ccycouncil review --repo /path/to/repo --range main..feature
ccycouncil review --repo /path/to/repo --commit abc1234
```

The council packages the selected diff, asks independent reviewers to produce
typed findings, challenges disputed findings, and aggregates evidence and
minority opinions. It does not silently broaden the selected Git scope.

## Competing implementation

`implement` creates disposable Git worktrees, gives multiple workers the same
task contract, verifies their results, and has independent roles compare the
candidates before an integrator produces the final patch.

```mermaid
flowchart TD
    A[Explicit repo, base, task, tests] --> B[Disposable worktrees]
    B --> C1[Worker candidate A]
    B --> C2[Worker candidate B]
    B --> C3[Worker candidate C]
    C1 --> D[Tests and evidence]
    C2 --> D
    C3 --> D
    D --> E[Peer review and judging]
    E --> F[Integrator candidate]
    F --> G[Acceptance checks]
    G --> H{semantic_commit?}
    H -->|yes| I[Eligible for ccycouncil apply]
    H -->|no| J[Abort; preserve evidence]
```

Example:

```sh
ccycouncil implement \
  --repo /path/to/repo \
  --base main \
  --task-file task.md \
  --test-command "pytest -q" \
  --worker-timeout 900
```

`apply` accepts only an implementation run with `semantic_commit`. It applies
the accepted patch to the requested repository but does not commit or push:

```sh
ccycouncil apply RUN_ID --repo /path/to/repo
```

Always inspect the resulting diff and rerun the repository's acceptance checks.

## Runs, replay, outcomes, and calibration

Run artifacts are private by default under
`~/.local/state/ccycouncil/runs/`. Useful commands include:

```sh
ccycouncil show RUN_ID
ccycouncil show RUN_ID --artifact verdict.json --json
ccycouncil replay RUN_ID
ccycouncil revisit RUN_ID --correction "What changed and why"
ccycouncil outcome RUN_ID correct --notes "Observed in production"
ccycouncil regrade RUN_ID --rules grading-rules.json
ccycouncil stats --json

ccycouncil anchors import anchors.json
ccycouncil anchors list --active
ccycouncil anchors validate
ccycouncil anchors retire ANCHOR_ID
```

Artifacts include task contracts, routing, typed claims, verification receipts,
judging data, dissent, finality, and outcome observations. Private filesystem
permissions reduce accidental local disclosure; they do not make provider-bound
model inputs private.

## Agent skill usage

The repository is also a distributable Codex-style skill:

- [`SKILL.md`](SKILL.md) is the canonical operating contract.
- [`agents/openai.yaml`](agents/openai.yaml) contains the agent-facing manifest.
- [`references/protocol.md`](references/protocol.md) defines the protocol.
- [`references/research.md`](references/research.md) and
  [`references/provenance.md`](references/provenance.md) document the design
  basis and source provenance.

The skill requires explicit invocation: ask to run `ccycouncil` or invoke the
installed `model-council` skill. It must not be triggered implicitly.

## What must never be committed

The `.gitignore` excludes common local and generated material, including:

- `.venv`, bytecode, caches, coverage, build output, logs, and editor state;
- `.env*`, proxy `config.yaml`, local YAML overrides, authentication folders,
  credentials, private keys, and certificates;
- council run artifacts, state, sync receipts, and generated evidence; and
- local agent scratch directories.

Before publishing a fork, also inspect the full Git history—not only the current
working tree—for names, email addresses, usernames, home-directory paths, host
names, account IDs, model-provider keys, bearer tokens, proxy credentials,
private URLs, prompts, source documents, and run evidence. See
[`PUBLIC_RELEASE_CHECKLIST.md`](PUBLIC_RELEASE_CHECKLIST.md).

## Development

```sh
uv sync --locked --dev
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q scripts tests
```

Contributions are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md). Security
reports should follow [`SECURITY.md`](SECURITY.md).

## License

MIT. See [`LICENSE`](LICENSE).
