# Source this helper from a zsh-based proxy launcher and call
# ccyproxy_catalogue_sync immediately before resolving smart aliases.

function ccyproxy_catalogue_sync {
  local proxy_config
  proxy_config="${CCYPROXY_CONFIG:-${HOME}/Library/Application Support/AIUsage/CLIProxyAPI/config.yaml}"

  if ! command -v ccycouncil >/dev/null 2>&1; then
    print -u2 -- "ccyproxy: warning: ccycouncil is unavailable; continuing with raw catalogue filtering"
    return 0
  fi

  if ! CCYPROXY_CONFIG="$proxy_config" command ccycouncil sync --json >/dev/null; then
    print -u2 -- "ccyproxy: warning: catalogue synchronization failed; continuing with raw catalogue filtering"
  fi
}

function ccyproxy_resolve_route {
  ccyproxy_catalogue_sync

  # Fetch /v1/models here, then intersect every alias candidate with the raw
  # model IDs before selecting a route. Never select a candidate on alias
  # membership alone.
}
