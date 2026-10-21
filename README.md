# Red Hat Subscription Discovery Tool

A small local web tool that inventories an estate for **Red Hat subscription and licensing assessments**:

Not affiliated with or endorsed by Red Hat or Broadcom/VMware. Red Hat, OpenShift and RHEL are trademarks of Red Hat, Inc.; VMware and vCenter are trademarks of Broadcom

| Source | Method | What is collected |
|---|---|---|
| **Red Hat OpenShift** | OpenShift / Kubernetes REST API (bearer token) | Cluster version, platform, cluster ID; every node with its role (**Master, Infra, Storage (ODF), Worker**), CPU, memory, disks, NICs, hardware vendor/model/serial, physical vs virtual; installed operators; ODF raw capacity |
| **OpenShift Virtualization** | Same API (KubeVirt / HCO CRDs) | Version; every VM with guest OS (guest agent or template), vCPU topology, memory, disks/PVCs, NICs/networks, node placement |
| **Red Hat Virtualization 4.x** | RHV Manager REST API v4 (`/ovirt-engine/api`) | Manager version, data centers, clusters; hypervisor hosts (RHV-H / RHEL-H, sockets/cores/threads, memory, NICs, vendor/model/serial); every VM with guest OS (guest agent or configured OS type), vCPU topology, memory, disks (provisioned/used, storage domain, interface), NICs (network, vNIC profile, MAC, IPs), cluster and host |
| **VMware vCenter** | vSphere API (pyVmomi, PropertyCollector) | ESXi hosts (sockets, cores, threads, memory, pNICs, cluster); every VM with guest OS (VMware Tools and configured), vCPU, memory, virtual disks, NICs, IPs |
| **RHEL (any Linux)** | SSH (paramiko), read-only script | OS release, CPU sockets/cores/threads, memory, physical disks, NICs (speed/MAC/IP/bond), DMI vendor/model/serial, physical vs virtual, `subscription-manager` status / SCA / Satellite vs CDN, add-ons (HA, SAP, RT) |

It then produces an **indicative subscription summary**: OpenShift core-pairs and socket-pairs, ODF, RHV hypervisor socket-pairs, RHEL physical socket-pairs, RHEL VMs per-guest vs. RHEL for Virtual Datacenters by vSphere / RHV cluster. RHEL systems are de-duplicated across SSH, vCenter, RHV and OpenShift Virtualization. OpenShift nodes that run as vCenter VMs (RHCOS) are excluded from the RHEL counts.

Exports: **Excel workbook** (10 sheets), **JSON** (to reload later), and **CSV** for any table.

---

## Quick start (macOS / Linux)

```bash
cd rh-discovery
./run.sh                    # creates .venv, installs requirements, opens http://127.0.0.1:8088
```

or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 server.py           # --port 9000, --bind 0.0.0.0 (only if you need remote access), --no-browser
```

Python 3.8+ is required. The UI uses no external CDNs, so it works on air-gapped jump hosts.

## Headless mode (jump host without a browser)

```bash
cp config.example.json targets.json      # edit
export OCP_PROD_TOKEN=sha256~...  VC_PASSWORD=...
python3 discover.py --config targets.json --out assessment.json --xlsx assessment.xlsx
```

Secrets in the config can be `env:VAR` or `file:/path` references. Copy `assessment.json` back to your laptop and open it in the web UI with **Load JSON**.

---

## Minimum (read-only) access

### OpenShift: service account with `cluster-reader`

```bash
oc create sa rh-discovery -n openshift-config
oc adm policy add-cluster-role-to-user cluster-reader -z rh-discovery -n openshift-config
oc create token rh-discovery -n openshift-config --duration=8h     # paste into the UI
# cleanup afterwards:  oc delete sa rh-discovery -n openshift-config
```

`oc whoami -t` from a cluster-admin session also works. Username/password login uses the `openshift-challenging-client` OAuth flow (HTPasswd/LDAP IdPs).

**Node hardware detail.** The Node API object has CPU and memory but not sockets, disks or NICs. The tool enriches nodes automatically from:
1. **BareMetalHost** inspection data (IPI / Assisted bare metal), which gives CPU model, disks, NICs with speed, and vendor/serial.
2. **NMState** `NodeNetworkState`, which gives NICs.
3. **Local Storage Operator** `LocalVolumeDiscoveryResult`, which gives disks.
4. **Optional node SSH** as `core` with the cluster's SSH key, which gives exact sockets/cores/disks/NICs. Use this for UPI or when sockets matter for bare-metal sizing. A bastion/jump host is supported.

### RHV Manager
A Manager user with the **ReadOnlyAdmin** role assigned at system level, in the `user@profile` form, e.g. `svc-ro@internal` or `svc-ro@corp.example.com` for an LDAP/IdM profile:

```bash
# on the Manager, for an internal-domain user:
ovirt-aaa-jdbc-tool user add svc-ro --attribute=firstName=Discovery
ovirt-aaa-jdbc-tool user password-reset svc-ro --password-valid-to="2027-12-31 00:00:00+0000"
# then Administration > Configure > System Permissions > Add: svc-ro, role ReadOnlyAdmin
```

Enter the Manager FQDN (for example `rhvm.example.com`) or the full API URL. The tool signs in through RHV SSO (with basic auth as a fallback). On RHV 4.2 and later, VM disks and NICs come back in bulk (`follow=`). On 4.0/4.1 it falls back to per-VM requests automatically.

VM guest OS and IPs come from the **guest agent** when it is running. Otherwise the VM's configured OS type (for example `rhel_8x64`) is used and flagged as `vm-config`.

### vCenter
A user with the built-in **Read-only** role, propagated from the vCenter root.

### RHEL hosts
Any SSH user. If that user has **passwordless sudo** (`sudo -n`), the tool also reads `subscription-manager` status and the DMI serial number. Without sudo those fields show "needs root". Nothing is installed or changed on the hosts. Host list format, one per line:

```
10.10.1.21
rhel-app01.example.com
admin@rhel-db01:2222   DB tier          # optional user@ / :port / label
```

---

## Sizing rules applied (see `rhdisco/licensing.py` → `RULES`)

| Item | Rule |
|---|---|
| OpenShift chargeable nodes | Nodes with the `worker` role that are **not** `infra`. Control-plane-only and infra-only nodes are excluded. Compact-cluster masters (master+worker) are counted. |
| OpenShift on VMs | Core-pair subscriptions = ceil(total worker vCPU ÷ 4)  (2 cores = 4 vCPU with SMT) |
| OpenShift bare metal | Per node: max(ceil(sockets ÷ 2), ceil(cores ÷ 64)). Unknown sockets are assumed to be 2 and flagged. |
| ODF | Storage-labelled nodes (`cluster.ocs.openshift.io/openshift-storage`), in core-pairs |
| RHEL physical | ceil(sockets ÷ 2) per host |
| RHEL VMs (per guest) | ceil(VMs ÷ 2). Powered-off VMs are excluded by default (`count_powered_off_vms`). |
| RHEL for Virtual Datacenters | Per vSphere or RHV cluster hosting RHEL VMs: Σ ceil(host sockets ÷ 2) |
| RHV hypervisors | ceil(sockets ÷ 2) per RHV host. The `HostedEngine` VM is not counted as a RHEL guest. Check whether the customer's RHV bundle already includes RHEL guest (VDC) rights. |
| RHEL on OpenShift Virtualization | Listed separately. Entitlement depends on the OpenShift offering. |

**These figures are estimates to support a true-up or renewal discussion. They are not a compliance verdict.** Validate them against the customer's agreement and current Red Hat product terms.

## Security notes
- Binds to `127.0.0.1` by default. Credentials are sent only to the local service, kept in memory for the duration of a job, then cleared. They are never written to disk or to the saved JSON.
- TLS verification is off by default because many internal APIs use private CAs. Tick **Verify TLS** where trusted certificates are in place.
- SSH host keys are auto-accepted (discovery use case). Run it from an approved jump host in regulated environments.
- The browser keeps the latest assessment in `localStorage` for convenience. Use **Save JSON** for anything you need to keep.

## Layout
```
server.py              Flask app + job runner (web UI backend)
discover.py            headless CLI
static/index.html      single-page UI (no external dependencies)
rhdisco/openshift.py   OpenShift + OpenShift Virtualization collector
rhdisco/vcenter.py     vCenter collector
rhdisco/rhv.py         RHV 4.x collector
rhdisco/rhel.py        SSH collector + discovery script
rhdisco/licensing.py   subscription sizing logic
rhdisco/export.py      Excel export
```
