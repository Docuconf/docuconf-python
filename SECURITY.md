# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/docuconf/docuconf-python/security/advisories/new)). Do not open a public issue, pull
request or discussion for a suspected vulnerability.

Include what you can of:

- the affected version of `docuconf-pydantic`, and of pydantic and pydantic-settings;
- what an attacker can do, and what they need first;
- steps or a minimal settings class, contract or environment that reproduces it.

We work on the fix in a private security advisory, credit you in it unless you prefer otherwise, and publish the
advisory when a fixed release is out.

## Response targets

| | |
|---|---|
| Acknowledge the report | within 3 business days |
| First assessment (confirmed or not, severity) | as soon as we can reproduce it, and we keep you updated in the advisory |
| Fix | released as a patch to the supported version, then the advisory is published |

## Supported versions

Security fixes go to the latest minor release of `docuconf-pydantic` on PyPI (tags `v*`), as a new patch release.

**During the beta, only the latest release is supported.** Upgrade to it to get a fix.

## Scope

In scope: the `docuconf-pydantic` package in [`src/docuconf`](src/docuconf), including its `docuconf` command, for
example a secret value that reaches an error message, a log line, the termination log, `repr()` or an exported
contract, or an input that passes the boot checks but should not.

Out of scope: the example applications under [`examples`](examples), vulnerabilities in dependencies that docuconf does
not make reachable (report those upstream), and issues in a platform or cluster that only arise from its own
misconfiguration. The CLI, the Go SDK, the Helm chart and the contract meta-schema live in
[docuconf-go](https://github.com/docuconf/docuconf-go), which has its own policy.

Releases are published to PyPI with trusted publishing and digital attestations; see [RELEASING.md](RELEASING.md).
