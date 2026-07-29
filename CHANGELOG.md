# Changelog

All notable changes to this project are documented here.

## 0.5.1 - 2026-07-29

- Update the exact `pypdf` production dependency pin from 6.6.0 to 6.14.2,
  resolving 58 Dependabot alerts, including four high-severity denial-of-service
  advisories for malformed PDF inputs.
- Update the exact `pytest` development dependency pin from 9.0.2 to 9.0.3,
  resolving the temporary-directory handling advisory.
- Retain the schema-v4 protocol and all 0.5.x compatibility guarantees,
  including the deprecated `ccycouncil` command and `model-council` skill alias.

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
