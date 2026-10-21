"""OpenShift + OpenShift Virtualization discovery via the Kubernetes/OpenShift REST API.

Read-only. A token bound to the `cluster-reader` ClusterRole is sufficient.

Node hardware detail (sockets, disks, NICs) is not part of the Node object, so it is enriched from
whichever of these sources exist in the cluster (best first):
  1. SSH to the node as `core` (optional, same parser as RHEL hosts)
  2. metal3 BareMetalHost inspection data (IPI / assisted bare metal)
  3. NMState NodeNetworkState (NICs) and Local Storage Operator LocalVolumeDiscoveryResult (disks)
"""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs

import requests
import urllib3

from .util import Logger, classify_os, now_iso, parse_cpu, parse_quantity

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROLE_PREFIX = "node-role.kubernetes.io/"
ODF_LABEL = "cluster.ocs.openshift.io/openshift-storage"


class OCPClient:
    def __init__(self, api_url, token=None, username=None, password=None, verify=False, timeout=30, log=None):
        self.api = api_url.rstrip("/")
        if not self.api.startswith("http"):
            self.api = "https://" + self.api
        self.verify = verify
        self.timeout = timeout
        self.log = log or Logger()
        self.s = requests.Session()
        self.s.verify = verify
        if not token and username:
            token = self._oauth_login(username, password)
        if not token:
            raise ValueError("A bearer token or username/password is required")
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def _oauth_login(self, username, password):
        meta = self.s.get(f"{self.api}/.well-known/oauth-authorization-server", timeout=self.timeout).json()
        auth = meta["authorization_endpoint"]
        r = self.s.get(auth, params={"response_type": "token", "client_id": "openshift-challenging-client"},
                       auth=(username, password), headers={"X-CSRF-Token": "1"}, allow_redirects=False,
                       timeout=self.timeout)
        loc = r.headers.get("Location", "")
        frag = parse_qs(urlparse(loc).fragment)
        if "access_token" not in frag:
            raise PermissionError(f"OAuth login failed (HTTP {r.status_code}). Check credentials or use a token.")
        self.log("Obtained OAuth token via username/password")
        return frag["access_token"][0]

    def get(self, path, params=None, quiet=False):
        r = self.s.get(self.api + path, params=params, timeout=self.timeout)
        if r.status_code == 401:
            raise PermissionError("HTTP 401 Unauthorized - token is invalid or expired")
        if r.status_code in (403, 404):
            if not quiet:
                self.log.warn(f"{path}: HTTP {r.status_code} ({'not installed' if r.status_code == 404 else 'forbidden'})")
            return None
        r.raise_for_status()
        return r.json()

    def list(self, path, params=None, quiet=False):
        items, params = [], dict(params or {})
        params.setdefault("limit", 500)
        while True:
            data = self.get(path, params=params, quiet=quiet)
            if data is None:
                return None
            items.extend(data.get("items", []))
            cont = (data.get("metadata") or {}).get("continue")
            if not cont:
                return items
            params["continue"] = cont


# --------------------------------------------------------------------------------------------- helpers

def _roles(labels):
    roles = sorted({k[len(ROLE_PREFIX):] for k in labels if k.startswith(ROLE_PREFIX)} - {""})
    if "control-plane" in roles and "master" not in roles:
        roles.append("master")
    if ODF_LABEL in labels:
        roles.append("storage")
    return sorted(set(roles))


def _primary_role(roles):
    if "master" in roles or "control-plane" in roles:
        return "Master" if "worker" not in roles else "Master+Worker"
    if "storage" in roles:
        return "Storage"
    if "infra" in roles:
        return "Infra"
    if "worker" in roles:
        return "Worker"
    return roles[0].title() if roles else "Unknown"


def _ready(node):
    for c in (node.get("status") or {}).get("conditions", []):
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def _addr(node, kind="InternalIP"):
    for a in (node.get("status") or {}).get("addresses", []):
        if a.get("type") == kind:
            return a.get("address")
    return ""


def _bmh_hw(bmh):
    hw = (bmh.get("status") or {}).get("hardware") or {}
    if not hw:
        return None
    cpu = hw.get("cpu") or {}
    sv = hw.get("systemVendor") or {}
    return {
        "source": "BareMetalHost",
        "cpu_model": cpu.get("model", ""),
        "logical_cpus": cpu.get("count"),
        "mhz": cpu.get("clockMegahertz"),
        "memory_bytes": (hw.get("ramMebibytes") or 0) * 1024 ** 2,
        "vendor": sv.get("manufacturer", ""),
        "model": sv.get("productName", ""),
        "serial": sv.get("serialNumber", ""),
        "disks": [{"name": d.get("name"), "size_bytes": d.get("sizeBytes"), "model": d.get("model", ""),
                   "vendor": d.get("vendor", ""), "rotational": d.get("rotational"), "type": d.get("type", ""),
                   "serial": d.get("serialNumber", "")} for d in hw.get("storage") or []],
        "nics": [{"name": n.get("name"), "mac": n.get("mac"), "ipv4": n.get("ip", ""),
                  "speed_mbps": int(n["speedGbps"] * 1000) if n.get("speedGbps") else None,
                  "model": n.get("model", ""), "physical": True} for n in hw.get("nics") or []],
        "bmc": (bmh.get("spec") or {}).get("bmc", {}).get("address", ""),
    }


def _nns_nics(nns):
    out = []
    for i in ((nns.get("status") or {}).get("currentState") or {}).get("interfaces", []):
        if i.get("type") not in ("ethernet", "bond"):
            continue
        name = i.get("name", "")
        if name.startswith(("veth", "genev", "ovn", "br-")):
            continue
        ips = [f"{a.get('ip')}/{a.get('prefix-length')}" for a in ((i.get("ipv4") or {}).get("address") or [])]
        eth = i.get("ethernet") or {}
        out.append({"name": name, "mac": i.get("mac-address", ""), "state": i.get("state", ""),
                    "speed_mbps": eth.get("speed"), "mtu": i.get("mtu"), "ipv4": ",".join(ips),
                    "physical": i.get("type") == "ethernet", "bond": "bond" if i.get("type") == "bond" else ""})
    return out


def _lvd_disks(lvd):
    out = []
    for d in (lvd.get("status") or {}).get("discoveredDevices", []):
        if d.get("type") not in ("disk", "mpath"):
            continue
        out.append({"name": (d.get("path") or "").replace("/dev/", ""), "size_bytes": d.get("size"),
                    "model": d.get("model", ""), "vendor": d.get("vendor", ""),
                    "rotational": d.get("property") == "Rotational", "serial": d.get("serial", ""),
                    "state": (d.get("status") or {}).get("state", "")})
    return out


# --------------------------------------------------------------------------------------------- Virtualization

def _vm_cpu_mem(vm, itypes):
    spec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
    dom = spec.get("domain") or {}
    cpu = dom.get("cpu") or {}
    sockets, cores, threads = cpu.get("sockets"), cpu.get("cores"), cpu.get("threads")
    vcpu = None
    if any((sockets, cores, threads)):
        vcpu = (sockets or 1) * (cores or 1) * (threads or 1)
    mem = parse_quantity((dom.get("memory") or {}).get("guest"))
    res = dom.get("resources") or {}
    if vcpu is None and (res.get("requests") or {}).get("cpu"):
        vcpu = parse_cpu(res["requests"]["cpu"])
    if mem is None:
        mem = parse_quantity((res.get("requests") or {}).get("memory"))
    it = (vm.get("spec") or {}).get("instancetype")
    itname = ""
    if it:
        itname = f"{it.get('kind', 'VirtualMachineClusterInstancetype')}/{it.get('name')}"
        d = itypes.get(it.get("name"))
        if d:
            vcpu = vcpu or (d.get("cpu") or {}).get("guest")
            mem = mem or parse_quantity((d.get("memory") or {}).get("guest"))
    return {"sockets": sockets or (1 if vcpu else None), "cores": cores, "threads": threads,
            "vcpu": vcpu, "memory_bytes": int(mem) if mem else None, "instancetype": itname}


def discover_virtualization(c, log, nodes_by_name):
    out = {"installed": False, "version": "", "vms": []}
    hco = c.list("/apis/hco.kubevirt.io/v1beta1/hyperconvergeds", quiet=True)
    kv = c.list("/apis/kubevirt.io/v1/kubevirts", quiet=True)
    if not hco and not kv:
        log("OpenShift Virtualization: not installed")
        return out
    out["installed"] = True
    for h in hco or []:
        for v in (h.get("status") or {}).get("versions", []) or []:
            if v.get("name") == "operator":
                out["version"] = v.get("version", "")
    if not out["version"] and kv:
        out["version"] = (kv[0].get("status") or {}).get("observedKubeVirtVersion", "")
    vms = c.list("/apis/kubevirt.io/v1/virtualmachines") or []
    vmis = {(i["metadata"]["namespace"], i["metadata"]["name"]): i
            for i in (c.list("/apis/kubevirt.io/v1/virtualmachineinstances") or [])}
    itypes = {}
    for path in ("/apis/instancetype.kubevirt.io/v1beta1/virtualmachineclusterinstancetypes",
                 "/apis/instancetype.kubevirt.io/v1beta1/virtualmachineinstancetypes"):
        for i in c.list(path, quiet=True) or []:
            itypes[i["metadata"]["name"]] = i.get("spec") or {}
    pvcs = {}
    if vms:
        for p in c.list("/api/v1/persistentvolumeclaims") or []:
            md = p["metadata"]
            cap = ((p.get("status") or {}).get("capacity") or {}).get("storage") or \
                  (((p.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
            pvcs[(md["namespace"], md["name"])] = (parse_quantity(cap) or 0, (p.get("spec") or {}).get("storageClassName", ""))
    for vm in vms:
        md = vm["metadata"]
        ns, name = md["namespace"], md["name"]
        vmi = vmis.get((ns, name)) or {}
        labels, ann = md.get("labels") or {}, md.get("annotations") or {}
        tpl_labels = ((((vm.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels")) or {}
        gos = (vmi.get("status") or {}).get("guestOSInfo") or {}
        os_hint = ann.get("vm.kubevirt.io/os", "")
        for k in list(labels) + list(tpl_labels):
            if k.startswith("os.template.kubevirt.io/"):
                os_hint = os_hint or k.split("/", 1)[1]
        pref = (vm.get("spec") or {}).get("preference") or {}
        if not os_hint and pref.get("name"):
            os_hint = pref["name"]
        os_text = gos.get("prettyName") or gos.get("name") or os_hint
        fam, ver = classify_os(os_text, gos.get("id", "") or os_hint)
        if gos.get("version") and fam in ("RHEL", "CentOS", "Rocky Linux", "AlmaLinux", "Oracle Linux"):
            ver = gos.get("version")
        tspec = ((vm.get("spec") or {}).get("template") or {}).get("spec") or {}
        # disks
        dvt = {d["metadata"]["name"]: d for d in (vm.get("spec") or {}).get("dataVolumeTemplates", []) or []}
        disks = []
        for v in tspec.get("volumes", []) or []:
            claim = (v.get("persistentVolumeClaim") or {}).get("claimName") or (v.get("dataVolume") or {}).get("name")
            if not claim:
                kind = next((k for k in v if k != "name"), "")
                disks.append({"name": v.get("name"), "source": kind, "size_bytes": None})
                continue
            size, sc = pvcs.get((ns, claim), (None, ""))
            if not size and claim in dvt:
                st = (dvt[claim].get("spec") or {}).get("storage") or (dvt[claim].get("spec") or {}).get("pvc") or {}
                size = parse_quantity(((st.get("resources") or {}).get("requests") or {}).get("storage"))
                sc = sc or st.get("storageClassName", "")
            disks.append({"name": v.get("name"), "source": claim, "size_bytes": int(size) if size else None, "storage_class": sc})
        nets = {n.get("name"): n for n in tspec.get("networks", []) or []}
        vmi_ifs = {i.get("name"): i for i in (vmi.get("status") or {}).get("interfaces", []) or []}
        nics = []
        for i in ((tspec.get("domain") or {}).get("devices") or {}).get("interfaces", []) or []:
            n = nets.get(i.get("name"), {})
            net = "pod" if "pod" in n else (n.get("multus") or {}).get("networkName", "")
            st = vmi_ifs.get(i.get("name"), {})
            binding = next((k for k in ("masquerade", "bridge", "sriov", "slirp") if k in i), (i.get("binding") or {}).get("name", ""))
            nics.append({"name": i.get("name"), "model": i.get("model", "virtio"), "binding": binding, "network": net,
                         "mac": st.get("mac") or i.get("macAddress", ""), "ipv4": st.get("ipAddress", "")})
        cm = _vm_cpu_mem(vm, itypes)
        node = (vmi.get("status") or {}).get("nodeName", "")
        out["vms"].append({
            "namespace": ns, "name": name,
            "status": (vm.get("status") or {}).get("printableStatus", "") or ("Running" if vmi else "Stopped"),
            "running": bool(vmi) and (vmi.get("status") or {}).get("phase") == "Running",
            "node": node, "node_role": (nodes_by_name.get(node) or {}).get("role", ""),
            "os_family": fam, "os_version": ver or "", "os_name": os_text or "",
            "os_source": "guest-agent" if gos else ("template/label" if os_hint else "unknown"),
            "guest_agent": bool(gos), "hostname": (vmi.get("status") or {}).get("guestOSInfo", {}).get("hostname", "") if gos else "",
            **cm, "disks": disks, "disk_total_bytes": sum(d.get("size_bytes") or 0 for d in disks), "nics": nics,
            "created": md.get("creationTimestamp", ""),
        })
    rhel = sum(1 for v in out["vms"] if v["os_family"] == "RHEL")
    log(f"OpenShift Virtualization {out['version']}: {len(out['vms'])} VM(s), {rhel} RHEL")
    return out


# --------------------------------------------------------------------------------------------- main

def discover_openshift(params, log=None):
    """params: api_url, token | username+password, verify_tls, name, node_ssh{enabled,user,key,password,passphrase,concurrency}"""
    log = log or Logger()
    c = OCPClient(params["api_url"], token=params.get("token"), username=params.get("username"),
                  password=params.get("password"), verify=params.get("verify_tls") in (True, "true", "1"), log=log)
    res = {"type": "openshift", "api_url": c.api, "discovered_at": now_iso(), "errors": []}

    cv = c.get("/apis/config.openshift.io/v1/clusterversions/version") or {}
    hist = (cv.get("status") or {}).get("history") or []
    res["version"] = ((cv.get("status") or {}).get("desired") or {}).get("version") or (hist[0].get("version") if hist else "")
    res["cluster_id"] = (cv.get("spec") or {}).get("clusterID", "")
    res["channel"] = (cv.get("spec") or {}).get("channel", "")
    infra = c.get("/apis/config.openshift.io/v1/infrastructures/cluster") or {}
    ist = infra.get("status") or {}
    res["platform"] = (ist.get("platformStatus") or {}).get("type") or ist.get("platform", "")
    res["infrastructure_name"] = ist.get("infrastructureName", "")
    res["topology"] = ist.get("controlPlaneTopology", "")
    res["name"] = params.get("name") or ist.get("infrastructureName", "").rsplit("-", 1)[0] or urlparse(c.api).hostname
    console = c.get("/apis/config.openshift.io/v1/consoles/cluster", quiet=True) or {}
    res["console_url"] = (console.get("status") or {}).get("consoleURL", "")
    log(f"Cluster {res['name']}: OpenShift {res['version']} on {res['platform'] or 'unknown platform'}")

    nodes = c.list("/api/v1/nodes") or []
    log(f"Found {len(nodes)} node(s)")

    # Enrichment sources
    bmhs = c.list("/apis/metal3.io/v1alpha1/baremetalhosts", quiet=True) or []
    machines = c.list("/apis/machine.openshift.io/v1beta1/machines", quiet=True) or []
    machine_node = {m["metadata"]["name"]: ((m.get("status") or {}).get("nodeRef") or {}).get("name") for m in machines}
    bmh_by_node = {}
    for b in bmhs:
        hw = _bmh_hw(b)
        if not hw:
            continue
        cons = ((b.get("spec") or {}).get("consumerRef") or {}).get("name")
        node_name = machine_node.get(cons) or ((b.get("status") or {}).get("hardware") or {}).get("hostname") or b["metadata"]["name"]
        bmh_by_node[node_name] = hw
    nns = {n["metadata"]["name"]: _nns_nics(n) for n in c.list("/apis/nmstate.io/v1beta1/nodenetworkstates", quiet=True) or []}
    lvd = {}
    for d in c.list("/apis/local.storage.openshift.io/v1alpha1/localvolumediscoveryresults", quiet=True) or []:
        lvd[(d.get("spec") or {}).get("nodeName")] = _lvd_disks(d)
    src = [s for s, v in (("BareMetalHost", bmh_by_node), ("NMState", nns), ("LocalVolumeDiscovery", lvd)) if v]
    log(f"Hardware enrichment sources available: {', '.join(src) or 'none (enable node SSH for disk/NIC/socket detail)'}")

    out_nodes = []
    for n in nodes:
        md, st = n["metadata"], n.get("status") or {}
        labels = md.get("labels") or {}
        roles = _roles(labels)
        cap, alloc, info = st.get("capacity") or {}, st.get("allocatable") or {}, st.get("nodeInfo") or {}
        pid = (n.get("spec") or {}).get("providerID", "")
        rec = {
            "name": md["name"], "roles": roles, "role": _primary_role(roles), "ready": _ready(n),
            "schedulable": not (n.get("spec") or {}).get("unschedulable", False),
            "taints": [f"{t.get('key')}:{t.get('effect')}" for t in (n.get("spec") or {}).get("taints", []) or []],
            "internal_ip": _addr(n), "hostname": _addr(n, "Hostname"),
            "os_image": info.get("osImage", ""), "kernel": info.get("kernelVersion", ""),
            "kubelet": info.get("kubeletVersion", ""), "runtime": info.get("containerRuntimeVersion", ""),
            "arch": info.get("architecture", ""), "system_uuid": info.get("systemUUID", ""),
            "cpu_capacity": parse_cpu(cap.get("cpu")), "cpu_allocatable": parse_cpu(alloc.get("cpu")),
            "memory_bytes": int(parse_quantity(cap.get("memory")) or 0),
            "memory_allocatable_bytes": int(parse_quantity(alloc.get("memory")) or 0),
            "ephemeral_storage_bytes": int(parse_quantity(cap.get("ephemeral-storage")) or 0),
            "pods_capacity": cap.get("pods"), "kubevirt_schedulable": labels.get("kubevirt.io/schedulable") == "true",
            "instance_type": labels.get("node.kubernetes.io/instance-type", ""),
            "zone": labels.get("topology.kubernetes.io/zone", ""),
            "provider_id": pid, "created": md.get("creationTimestamp", ""),
            "hw": {"sources": []},
        }
        hw = rec["hw"]
        b = bmh_by_node.get(md["name"])
        if b:
            hw.update({k: v for k, v in b.items() if k != "source"})
            hw["sources"].append("BareMetalHost")
        if not hw.get("nics") and nns.get(md["name"]):
            hw["nics"] = nns[md["name"]]
            hw["sources"].append("NMState")
        if not hw.get("disks") and lvd.get(md["name"]):
            hw["disks"] = lvd[md["name"]]
            hw["sources"].append("LocalVolumeDiscovery")
        # physical / virtual classification
        plat = (res["platform"] or "").lower()
        if b or plat == "baremetal":
            rec["virtualization"] = "none"  # metal3 inspected host, or BareMetal platform
        elif pid.startswith("vsphere://") or plat == "vsphere":
            rec["virtualization"] = "vmware"
        elif pid.startswith(("aws://", "azure://", "gce://", "openstack://", "ibm://", "kubevirt://")) or \
                plat in ("aws", "azure", "gcp", "openstack", "ibmcloud", "kubevirt", "nutanix", "ovirt"):
            rec["virtualization"] = plat or pid.split(":")[0]
        else:
            rec["virtualization"] = "unknown"
        out_nodes.append(rec)

    # Optional SSH enrichment (core@node)
    ns = params.get("node_ssh") or {}
    if ns.get("enabled") and (ns.get("key") or ns.get("password")):
        from .rhel import discover_host
        creds = {"key": ns.get("key"), "password": ns.get("password"), "passphrase": ns.get("passphrase"), "sudo": True, "timeout": 20}
        jump = ns.get("jump") if (ns.get("jump") or {}).get("host") else None
        log(f"SSH enrichment of {len(out_nodes)} node(s) as '{ns.get('user') or 'core'}'")

        def one(rec):
            t = {"host": rec["internal_ip"] or rec["name"], "port": int(ns.get("port") or 22), "user": ns.get("user") or "core"}
            h = discover_host(t, creds, log, jump)
            if h.get("status") == "error":
                rec["hw"]["ssh_error"] = h.get("error")
                return
            cpu = h.get("cpu") or {}
            rec["hw"].update({
                "cpu_model": cpu.get("model"), "sockets": cpu.get("sockets"), "cores_per_socket": cpu.get("cores_per_socket"),
                "threads_per_core": cpu.get("threads_per_core"), "physical_cores": cpu.get("physical_cores"),
                "logical_cpus": cpu.get("logical_cpus"), "vendor": h["hardware"].get("vendor"), "model": h["hardware"].get("model"),
                "serial": h["hardware"].get("serial") or rec["hw"].get("serial"), "disks": h.get("disks"), "nics": h.get("nics"),
            })
            rec["hw"]["sources"].insert(0, "SSH")
            rec["virtualization"] = h.get("virtualization", rec["virtualization"])

        with ThreadPoolExecutor(max_workers=int(ns.get("concurrency") or 8)) as ex:
            list(ex.map(one, out_nodes))

    for r in out_nodes:
        hw = r["hw"]
        r["is_virtual"] = r["virtualization"] not in ("none", "unknown")
        r["disk_total_bytes"] = sum((d.get("size_bytes") or 0) for d in hw.get("disks") or [])
        r["nic_count"] = len([x for x in hw.get("nics") or [] if x.get("physical", True)])
        if not hw.get("sockets") and r["virtualization"] == "none":
            hw["sockets_note"] = "unknown - enable node SSH"
    res["nodes"] = out_nodes
    by_name = {r["name"]: r for r in out_nodes}

    # Installed operators (for add-on subscriptions such as ODF, ACM, ACS, Quay, Virtualization)
    csvs = c.list("/apis/operators.coreos.com/v1alpha1/clusterserviceversions",
                  params={"labelSelector": "!olm.copiedFrom"}, quiet=True) or []
    seen, ops = set(), []
    for x in csvs:
        md, sp = x["metadata"], x.get("spec") or {}
        key = (md["name"], md["namespace"])
        if key in seen:
            continue
        seen.add(key)
        ops.append({"name": md["name"], "display_name": sp.get("displayName", ""), "version": sp.get("version", ""),
                    "namespace": md["namespace"], "provider": (sp.get("provider") or {}).get("name", ""),
                    "phase": (x.get("status") or {}).get("phase", "")})
    res["operators"] = sorted(ops, key=lambda o: o["display_name"] or o["name"])
    log(f"{len(ops)} installed operator(s)")

    # ODF / Ceph capacity
    odf = {"installed": False}
    scs = c.list("/apis/ocs.openshift.io/v1/storageclusters", quiet=True)
    if scs:
        odf["installed"] = True
        odf["storageclusters"] = [{"name": s["metadata"]["name"], "phase": (s.get("status") or {}).get("phase", "")} for s in scs]
        for cc in c.list("/apis/ceph.rook.io/v1/cephclusters", quiet=True) or []:
            cap = ((cc.get("status") or {}).get("ceph") or {}).get("capacity") or {}
            odf["raw_capacity_bytes"] = cap.get("bytesTotal")
            odf["raw_used_bytes"] = cap.get("bytesUsed")
            odf["health"] = ((cc.get("status") or {}).get("ceph") or {}).get("health", "")
        log(f"ODF installed: raw capacity {round((odf.get('raw_capacity_bytes') or 0) / 1024 ** 4, 2)} TiB")
    res["odf"] = odf

    res["virtualization"] = discover_virtualization(c, log, by_name)
    return res
