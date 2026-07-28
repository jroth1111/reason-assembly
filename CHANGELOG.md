# Changelog

All notable changes to this project are documented here.

## 0.5.0 - 2026-07-28

- Rename the product, package, skill, and canonical command to Reason Assembly
  and `reason-assembly` while retaining `ccycouncil` as a deprecated forwarding
  command and `model-council` as an explicit deprecated skill alias.
- Move canonical private state to `~/.local/state/reason-assembly` and
  non-destructively copy missing legacy files from
  `~/.local/state/ccycouncil`; preserve the legacy tree, keep canonical files
  on collisions, import only terminal stable runs through staged atomic publication,
  never merge colliding run IDs, and retain read-only discovery for incomplete or
  subsequently changed legacy runs.
- Prefer canonical `REASON_ASSEMBLY_*` environment variables while accepting
  documented `CCYCOUNCIL_*` compatibility names during the deprecation period;
  isolate custom state roots unless a legacy root is explicitly supplied, and keep
  catalogue sync receipts under the configured application state root.
- Reserve run IDs with atomic directory creation and retry collisions without
  altering existing evidence; initialize shared schema-v4 state with guarded,
  create-if-absent writes so concurrent startup cannot overwrite prior state.
- Build canonical wheels and console scripts through setuptools, with locked CI
  coverage for wheel installation plus canonical and deprecated command smoke
  tests on Python 3.11 through 3.13.
- Update ccyproxy integration to synchronize through `reason-assembly` first,
  fall back to deprecated `ccycouncil` when necessary, and continue enforcing
  the raw model catalogue as the routing authority.
- Keep the deprecated command and skill aliases throughout the 0.5.x line; any
  removal will be announced in a future changelog before it takes effect.

## 0.4.1 - 2026-07-28

- Validate raw and capability-metadata catalogues on every council catalogue
  read, with one retry for transient mismatches.
- Keep raw `/v1/models` IDs authoritative when a mismatch persists and mark
  unmatched raw models as listed-only.
- Atomically prune absent smart-alias candidates under an exclusive lock while
  preserving unrelated proxy configuration.
- Add `ccycouncil sync`, private schema-v4 synchronization receipts, and sync
  diagnostics in `doctor --all-models`.
- Refresh the proxy catalogue before launcher route resolution while retaining
  raw-ID runtime filtering.
- Publish the project documentation, safety guidance, public integration
  example, and continuous-integration workflow.
