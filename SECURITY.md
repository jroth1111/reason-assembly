# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected security vulnerability or for
anything that contains credentials, private prompts, proxy configuration, run
artifacts, provider responses, or personal information.

Use GitHub's private vulnerability reporting for this repository:

1. Open the repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Include the affected version, a minimal reproduction with synthetic data, the
security impact, and any suggested mitigation. Remove API keys, bearer tokens,
cookie values, local paths, account identifiers, and private host names before
submitting.

## Supported versions

Security fixes are made on the current `main` branch and in the latest release.
Older snapshots are not guaranteed to receive backports.

## Scope reminders

`reason-assembly` sends prompts and selected context to the providers configured in
your proxy. Local permission hardening protects files on disk but does not make
provider-bound inputs private. Verification commands and implementation tasks
can execute local processes; only provide commands and repositories you trust.
