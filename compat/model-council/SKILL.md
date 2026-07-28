---
name: model-council
description: Deprecated compatibility alias for Reason Assembly. Use only when the user explicitly invokes $model-council; direct new usage to $reason-assembly.
disable-model-invocation: true
user-invocable: true
---

# Model Council compatibility alias

This skill name is deprecated. Delegate to the installed `$reason-assembly` skill
and invoke the `reason-assembly` command. If the canonical skill is unavailable,
stop and ask the user to install it rather than duplicating or weakening its
contract. Do not invoke the `ccycouncil` compatibility launcher for routine
operation because it intentionally emits a deprecation warning.
