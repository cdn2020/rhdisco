# Security Policy

RH Discovery Tool connects to production infrastructure (OpenShift, Red Hat Virtualization, VMware vCenter and RHEL hosts over SSH) with privileged read credentials, and the inventories it produces are sensitive. Security reports are taken seriously and handled privately.

## Supported versions

Only the latest release on the `main` branch receives security fixes. Please upgrade before you report.

| Version | Supported |
|---|---|
| latest `main` / most recent release | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Report privately through GitHub's private vulnerability reporting:
**[Report a vulnerability](https://github.com/cdn2020/rhdisco/security/advisories/new)** (repository → *Security* tab → *Report a vulnerability*).

Please include:
- the affected version or commit, and how you run the tool (web UI or `discover.py`), with Python version and OS
- a description of the issue and its impact
- steps to reproduce or a proof of concept, using **test or lab systems only**
- any suggested fix, if you have one

**Never include real credentials, tokens, private keys, or customer inventory data** (hostnames, IPs, serial numbers, exported JSON/Excel files) in a report. Redact or replace them with dummy values.

### What to expect

This is a community project maintained on a best-effort basis:

| Step | Target |
|---|---|
| Acknowledgement of your report | within 5 business days |
| Initial assessment and severity | within 10 business days |
| Fix or mitigation for critical/high issues | as soon as practical, aiming for 30 days |

You will be kept informed of progress. Once a fix is released, a GitHub Security Advisory will be published. If you wish, you will be credited as the reporter.

Please allow a reasonable time for a fix before disclosing publicly (coordinated disclosure, normally up to 90 days).

## Scope

**In scope**
- Exposure or leakage of credentials, tokens or keys handled by the tool (in memory, logs, job results, exports or browser storage)
- Vulnerabilities in the local web service (`server.py`): unauthorised access, request forgery, injection, path traversal
- Cross-site scripting or HTML injection in the web UI, including through data returned by discovered systems (VM names, hostnames, OS strings, labels)
- Any action that modifies a target system. The tool is designed to be strictly read-only.
- Command injection in the SSH discovery script or host-list parsing
- Unsafe handling of loaded assessment JSON files

**Out of scope**
- Vulnerabilities in the target platforms themselves (OpenShift, RHV, vCenter, RHEL). Report those to the respective vendors.
- Vulnerabilities in third-party dependencies with no demonstrated exploit path through this tool. Report them upstream; we will update the pinned versions.
- The documented, deliberate defaults listed below, unless you show a way to exploit them beyond what is described
- Issues that require an attacker who already has local administrator/root access to the machine running the tool
- Accuracy of subscription estimates. That is a functional issue, not a security one; please open a normal issue.

## Security model and safe-use guidance

How the tool is designed to behave:

- **Read-only.** All discovery is done with read APIs (OpenShift `cluster-reader`, RHV `ReadOnlyAdmin`, vCenter Read-only role) and a non-modifying shell script over SSH. The SSH script is in `rhdisco/rhel.py` (`DISCOVERY_SCRIPT`), so you can review it before use.
- **Local only by default.** The web service binds to `127.0.0.1`. Do not use `--bind 0.0.0.0` on a shared or untrusted network. The service has no authentication of its own.
- **Credentials in memory only.** Credentials are sent from the browser to the local service, used for the duration of a discovery job, then cleared. They are not written to disk, logs, saved JSON or Excel exports. In headless mode use `env:` or `file:` references rather than literal secrets in config files.

Documented defaults you should be aware of:

- **TLS certificate verification is off by default**, because many internal APIs use private CAs. Tick *Verify TLS* (or set `"verify_tls": true`) wherever trusted certificates are in place.
- **SSH host keys are accepted automatically** (no `known_hosts` check). Run discovery from a trusted jump host on a trusted network.
- **Browser local storage** keeps the most recent assessment (inventory only, no credentials) for convenience. Clear it on shared machines.

Recommended practice, especially in regulated environments (e.g. PCI-DSS, ISO 27001):

1. Use **dedicated, least-privilege, time-limited** accounts and tokens for each engagement, and revoke them afterwards (e.g. `oc create token ... --duration=8h`, then delete the service account).
2. Run the tool from an **approved jump host**, following the customer's change and access procedures.
3. Treat assessment JSON and Excel exports as **confidential**: they contain hostnames, IP addresses, serial numbers and topology. Store them encrypted, share them only through approved channels, and **never commit them to Git**. The repository's `.gitignore` excludes common export filenames.
4. Keep dependencies up to date (`pip install -U -r requirements.txt`) and enable Dependabot alerts on forks.

## Disclaimer

This software is provided "as is", without warranty of any kind, under the terms of the [Apache License 2.0](LICENSE). It is not affiliated with or endorsed by Red Hat, Inc. or Broadcom Inc.
