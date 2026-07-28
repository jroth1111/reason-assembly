# Changelog

All notable changes to this project are documented here.

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
