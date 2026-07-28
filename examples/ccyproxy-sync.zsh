# Source this helper from a zsh-based proxy launcher and call
# ccyproxy_catalogue_sync immediately before resolving smart aliases.

function ccyproxy_catalogue_sync {
  local proxy_config council_command
  proxy_config="${CCYPROXY_CONFIG:-${HOME}/Library/Application Support/AIUsage/CLIProxyAPI/config.yaml}"

  if command -v reason-assembly >/dev/null 2>&1; then
    council_command=reason-assembly
  elif command -v ccycouncil >/dev/null 2>&1; then
    council_command=ccycouncil
    if [[ -z "${_REASON_ASSEMBLY_LEGACY_NOTICE:-}" ]]; then
      print -u2 -- "ccyproxy: warning: using deprecated ccycouncil fallback; install reason-assembly"
      typeset -g _REASON_ASSEMBLY_LEGACY_NOTICE=1
    fi
  else
    print -u2 -- "ccyproxy: warning: reason-assembly is unavailable; continuing with raw catalogue filtering"
    return 0
  fi

  if [[ "$council_command" == ccycouncil ]]; then
    if ! REASON_ASSEMBLY_SUPPRESS_DEPRECATION=1 CCYPROXY_CONFIG="$proxy_config" command ccycouncil sync --json >/dev/null; then
      print -u2 -- "ccyproxy: warning: catalogue synchronization failed; continuing with raw catalogue filtering"
    fi
  elif ! CCYPROXY_CONFIG="$proxy_config" command reason-assembly sync --json >/dev/null; then
    print -u2 -- "ccyproxy: warning: catalogue synchronization failed; continuing with raw catalogue filtering"
  fi
}

function ccyproxy_resolve_route {
  ccyproxy_catalogue_sync

  # Fetch /v1/models here, then intersect every alias candidate with the raw
  # model IDs before selecting a route. Never select a candidate on alias
  # membership alone.
}
