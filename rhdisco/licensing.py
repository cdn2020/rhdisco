"""Subscription sizing estimates built from discovery results.

These are indicative counts to support a true-up / renewal conversation, NOT a compliance verdict.
Rules applied (editable in RULES below):
  * OpenShift: chargeable nodes = nodes carrying the worker role that are not dedicated infra nodes
    (control-plane only and infra-only nodes are excluded). Compact clusters (master+worker) are chargeable.
      - Virtual nodes:   Core-pair subscriptions = ceil(total vCPU / 4)  (2 cores == 4 vCPU with SMT)
      - Bare-metal nodes: Socket-pair subscriptions per node = max(ceil(sockets/2), ceil(cores/64))
  * ODF: storage-labelled nodes, sized in core-pairs (vCPU/4 virtual, physical cores/2 bare metal).
  * RHEL physical: ceil(sockets/2) per host (2-socket pair).
  * RHEL virtual (per-guest model): ceil(VMs/2) (one subscription covers up to 2 guests).
  * RHEL for Virtual Datacenters (alternative): per hypervisor host ceil(sockets/2) for every host in a vSphere
    or RHV cluster that runs RHEL guests (unlimited RHEL guests on those hosts).
  * RHV hypervisors: ceil(sockets/2) per RHV host (socket-pair). The RHV HostedEngine VM is not counted as a RHEL guest.
"""
from collections import Counter, defaultdict

from .util import ceil_div, now_iso, short_name

RULES = {
    "vcpu_per_core_pair": 4,
    "cores_per_bm_socket_pair": 64,
    "rhel_guests_per_sub": 2,
    "assume_sockets_if_unknown": 2,
    "count_powered_off_vms": False,
}


def _ocp_node_names(assessment):
    names = set()
    for c in assessment.get("openshift", []):
        for n in c.get("nodes", []):
            names.add(short_name(n["name"]))
            if n.get("hostname"):
                names.add(short_name(n["hostname"]))
    return names


def _vc_vm_index(assessment):
    """name/hostname/IP -> hypervisor platform for every VM seen in vCenter or RHV."""
    idx = {}
    for plat, key in (("vmware", "vcenter"), ("rhv", "rhv")):
        for src in assessment.get(key, []) or []:
            for vm in src.get("vms", []):
                for k in (vm.get("name"), vm.get("hostname")):
                    if k:
                        idx[short_name(k)] = plat
                if vm.get("ip"):
                    idx[vm["ip"]] = plat
    return idx


def summarize_openshift(assessment, vc_idx):
    out = []
    for c in assessment.get("openshift", []):
        roles = defaultdict(lambda: {"nodes": 0, "vcpu": 0, "memory_bytes": 0, "disk_bytes": 0, "physical": 0, "virtual": 0})
        ch = {"nodes": 0, "virtual_nodes": 0, "virtual_vcpu": 0, "core_pair_subs": 0,
              "bm_nodes": 0, "bm_sockets": 0, "bm_cores": 0, "bm_socket_pair_subs": 0, "bm_unknown_socket_nodes": 0,
              "unknown_nodes": 0, "unknown_vcpu": 0}
        odf = {"nodes": 0, "vcpu": 0, "physical_cores": 0, "core_pair_subs": 0}
        node_rows = []
        for n in c.get("nodes", []):
            virt = n.get("virtualization", "unknown")
            if virt == "unknown" and short_name(n["name"]) in vc_idx:
                virt = vc_idx[short_name(n["name"])]  # matched to a vCenter / RHV VM
            hw = n.get("hw") or {}
            r = roles[n["role"]]
            r["nodes"] += 1
            r["vcpu"] += n.get("cpu_capacity") or 0
            r["memory_bytes"] += n.get("memory_bytes") or 0
            r["disk_bytes"] += n.get("disk_total_bytes") or 0
            r["physical" if virt == "none" else "virtual"] += 1
            rl = set(n.get("roles", []))
            chargeable = "worker" in rl and "infra" not in rl and not ("storage" in rl and "worker" not in rl)
            reason = "worker" if chargeable else ("control-plane" if ("master" in rl or "control-plane" in rl) else
                                                  "infra" if "infra" in rl else "storage" if "storage" in rl else "no worker role")
            subs = None
            if chargeable:
                ch["nodes"] += 1
                if virt == "none":
                    sockets = hw.get("sockets")
                    cores = hw.get("physical_cores") or 0
                    if not sockets:
                        ch["bm_unknown_socket_nodes"] += 1
                        sockets = RULES["assume_sockets_if_unknown"]
                    subs = max(ceil_div(sockets, 2), ceil_div(cores, RULES["cores_per_bm_socket_pair"]))
                    ch["bm_nodes"] += 1
                    ch["bm_sockets"] += sockets
                    ch["bm_cores"] += cores
                    ch["bm_socket_pair_subs"] += subs
                elif virt == "unknown":
                    ch["unknown_nodes"] += 1
                    ch["unknown_vcpu"] += n.get("cpu_capacity") or 0
                else:
                    ch["virtual_nodes"] += 1
                    ch["virtual_vcpu"] += n.get("cpu_capacity") or 0
            if "storage" in rl:
                odf["nodes"] += 1
                odf["vcpu"] += n.get("cpu_capacity") or 0
                odf["physical_cores"] += hw.get("physical_cores") or 0
            node_rows.append({"name": n["name"], "role": n["role"], "virtualization": virt, "chargeable": chargeable,
                              "reason": reason, "vcpu": n.get("cpu_capacity"), "socket_pair_subs": subs})
        ch["core_pair_subs"] = ceil_div(ch["virtual_vcpu"], RULES["vcpu_per_core_pair"])
        ch["unknown_core_pair_subs_if_virtual"] = ceil_div(ch["unknown_vcpu"], RULES["vcpu_per_core_pair"])
        odf_installed = (c.get("odf") or {}).get("installed")
        if odf_installed:
            odf["core_pair_subs"] = ceil_div(odf["physical_cores"], 2) if odf["physical_cores"] else ceil_div(odf["vcpu"], RULES["vcpu_per_core_pair"])
        v = c.get("virtualization") or {}
        vms = v.get("vms", [])
        out.append({
            "name": c.get("name"), "api_url": c.get("api_url"), "version": c.get("version"), "platform": c.get("platform"),
            "cluster_id": c.get("cluster_id"), "roles": dict(roles), "chargeable": ch, "nodes": node_rows,
            "odf": {"installed": bool(odf_installed), **odf, "raw_capacity_bytes": (c.get("odf") or {}).get("raw_capacity_bytes")},
            "virtualization": {"installed": v.get("installed", False), "version": v.get("version", ""), "vms": len(vms),
                               "running": sum(1 for x in vms if x.get("running")),
                               "rhel_vms": sum(1 for x in vms if x.get("os_family") == "RHEL"),
                               "windows_vms": sum(1 for x in vms if x.get("os_family") == "Windows"),
                               "other_vms": sum(1 for x in vms if x.get("os_family") not in ("RHEL", "Windows")),
                               "vcpu": sum(x.get("vcpu") or 0 for x in vms)},
            "operators": [o.get("display_name") or o.get("name") for o in c.get("operators", [])],
        })
    return out


def build_rhel_estate(assessment, ocp_names):
    """Merge SSH-discovered hosts and vCenter RHEL VMs into a de-duplicated RHEL inventory."""
    estate, keys = [], {}

    def key_of(*vals):
        for v in vals:
            if v and (short_name(v) in keys or v in keys):
                return keys.get(short_name(v)) or keys.get(v)
        return None

    for block in assessment.get("rhel", []):
        for h in block.get("hosts", []):
            if h.get("status") == "error" or h.get("os_family") != "RHEL":
                continue
            cpu = h.get("cpu") or {}
            e = {"name": h.get("hostname") or h["target"], "sources": ["SSH"], "os_version": h.get("os_version", ""),
                 "virtual": h.get("is_virtual", False), "platform": h.get("virtualization", ""),
                 "sockets": cpu.get("sockets"), "cores": cpu.get("physical_cores"), "vcpu": cpu.get("logical_cpus"),
                 "memory_bytes": h.get("memory_bytes"), "subscription": (h.get("subscription") or {}).get("overall_status", ""),
                 "registered_to": (h.get("subscription") or {}).get("registered_to", ""), "addons": h.get("addons", []),
                 "cluster": "", "power_state": "poweredOn", "ip": (h.get("target") or "")}
            estate.append(e)
            for k in (short_name(e["name"]), short_name(h["target"]), h["target"]):
                keys[k] = e
    for vc in assessment.get("vcenter", []):
        for vm in vc.get("vms", []):
            if vm.get("os_family") != "RHEL":
                continue
            if short_name(vm.get("name")) in ocp_names or short_name(vm.get("hostname")) in ocp_names:
                continue  # RHCOS/RHEL OpenShift node - covered by the OpenShift subscription
            ex = key_of(vm.get("hostname"), vm.get("name"), vm.get("ip"))
            if ex:
                ex["sources"].append(f"vCenter:{vc.get('name')}")
                ex.update({"virtual": True, "platform": "vmware", "cluster": vm.get("cluster"), "power_state": vm.get("power_state"),
                           "vcenter": vc.get("name"), "esxi_host": vm.get("esxi_host")})
                continue
            e = {"name": vm.get("hostname") or vm.get("name"), "vm_name": vm.get("name"), "sources": [f"vCenter:{vc.get('name')}"],
                 "os_version": vm.get("os_version", ""), "virtual": True, "platform": "vmware", "sockets": None, "cores": None,
                 "vcpu": vm.get("vcpu"), "memory_bytes": vm.get("memory_bytes"), "subscription": "", "registered_to": "",
                 "addons": [], "cluster": vm.get("cluster"), "power_state": vm.get("power_state"), "ip": vm.get("ip"),
                 "vcenter": vc.get("name"), "datacenter": vm.get("datacenter"), "esxi_host": vm.get("esxi_host")}
            estate.append(e)
            for k in (short_name(vm.get("name")), short_name(vm.get("hostname")), vm.get("ip")):
                if k:
                    keys[k] = e
    for rv in assessment.get("rhv", []) or []:
        for vm in rv.get("vms", []):
            if vm.get("os_family") != "RHEL" or vm.get("hosted_engine"):
                continue  # HostedEngine appliance is part of the RHV subscription
            if short_name(vm.get("name")) in ocp_names or short_name(vm.get("hostname")) in ocp_names:
                continue
            info = {"virtual": True, "platform": "rhv", "cluster": vm.get("cluster"), "power_state": vm.get("power_state"),
                    "rhv": rv.get("name"), "datacenter": vm.get("datacenter"), "hypervisor": vm.get("host")}
            ex = key_of(vm.get("hostname"), vm.get("name"), vm.get("ip"))
            if ex:
                ex["sources"].append(f"RHV:{rv.get('name')}")
                ex.update(info)
                continue
            e = {"name": vm.get("hostname") or vm.get("name"), "vm_name": vm.get("name"), "sources": [f"RHV:{rv.get('name')}"],
                 "os_version": vm.get("os_version", ""), "sockets": None, "cores": None, "vcpu": vm.get("vcpu"),
                 "memory_bytes": vm.get("memory_bytes"), "subscription": "", "registered_to": "", "addons": [], "ip": vm.get("ip"), **info}
            estate.append(e)
            for k in (short_name(vm.get("name")), short_name(vm.get("hostname")), vm.get("ip")):
                if k:
                    keys[k] = e
    for c in assessment.get("openshift", []):
        for vm in (c.get("virtualization") or {}).get("vms", []):
            if vm.get("os_family") == "RHEL":
                estate.append({"name": vm.get("hostname") or f"{vm['namespace']}/{vm['name']}", "sources": [f"OCP-Virt:{c.get('name')}"],
                               "os_version": vm.get("os_version", ""), "virtual": True, "platform": "openshift-virtualization",
                               "vcpu": vm.get("vcpu"), "memory_bytes": vm.get("memory_bytes"), "cluster": c.get("name"),
                               "power_state": "poweredOn" if vm.get("running") else "poweredOff", "subscription": "",
                               "registered_to": "", "addons": [], "sockets": None, "cores": None})
    return estate


def summarize(assessment):
    vc_idx = _vc_vm_index(assessment)
    ocp_names = _ocp_node_names(assessment)
    ocp = summarize_openshift(assessment, vc_idx)
    estate = build_rhel_estate(assessment, ocp_names)
    count_off = RULES["count_powered_off_vms"]

    def active(e):
        return count_off or e.get("power_state") in (None, "", "poweredOn")

    phys = [e for e in estate if not e["virtual"]]
    phys_unknown = sum(1 for e in phys if not e.get("sockets"))
    phys_subs = sum(ceil_div(e.get("sockets") or RULES["assume_sockets_if_unknown"], 2) for e in phys)
    vmw = [e for e in estate if e["virtual"] and e.get("platform") == "vmware" and active(e)]
    ocpv = [e for e in estate if e.get("platform") == "openshift-virtualization"]
    rhvl = [e for e in estate if e["virtual"] and e.get("platform") == "rhv" and active(e)]
    other_virt = [e for e in estate if e["virtual"] and e.get("platform") not in ("vmware", "rhv", "openshift-virtualization") and active(e)]
    powered_off = sum(1 for e in estate if e["virtual"] and not active(e))

    # VDC alternative per vSphere cluster
    vdc = []
    rhel_by_cluster = Counter((e.get("vcenter"), e.get("cluster")) for e in vmw)
    for vc in assessment.get("vcenter", []):
        for cl in vc.get("clusters", []):
            n = rhel_by_cluster.get((vc.get("name"), cl["cluster"]), 0)
            if not n:
                continue
            hosts = [h for h in vc.get("hosts", []) if h["cluster"] == cl["cluster"] and h["datacenter"] == cl["datacenter"]]
            if cl["cluster"] == "(standalone)":
                hosts = [h for h in hosts if any(e.get("esxi_host") == h["name"] for e in vmw)]
            subs = sum(ceil_div(h.get("sockets") or 2, 2) for h in hosts)
            vdc.append({"vcenter": vc.get("name"), "datacenter": cl["datacenter"], "cluster": cl["cluster"], "hosts": len(hosts),
                        "sockets": sum(h.get("sockets") or 0 for h in hosts), "rhel_vms": n,
                        "per_guest_subs": ceil_div(n, RULES["rhel_guests_per_sub"]), "vdc_subs": subs})

    # RHV: hypervisor socket-pairs and VDC alternative per RHV cluster
    rhv_vdc, rhv_hosts = [], []
    rhel_by_rhv_cluster = Counter((e.get("rhv"), e.get("datacenter"), e.get("cluster")) for e in rhvl)
    for rv in assessment.get("rhv", []) or []:
        rhv_hosts += rv.get("hosts", [])
        for cl in rv.get("clusters", []):
            n = rhel_by_rhv_cluster.get((rv.get("name"), cl["datacenter"], cl["cluster"]), 0)
            if not n:
                continue
            hosts = [h for h in rv.get("hosts", []) if h["cluster"] == cl["cluster"] and h["datacenter"] == cl["datacenter"]]
            rhv_vdc.append({"rhv": rv.get("name"), "datacenter": cl["datacenter"], "cluster": cl["cluster"], "hosts": len(hosts),
                            "sockets": sum(h.get("sockets") or 0 for h in hosts), "rhel_vms": n,
                            "per_guest_subs": ceil_div(n, RULES["rhel_guests_per_sub"]),
                            "vdc_subs": sum(ceil_div(h.get("sockets") or 2, 2) for h in hosts)})
    rhv_hyp_subs = sum(ceil_div(h.get("sockets") or RULES["assume_sockets_if_unknown"], 2) for h in rhv_hosts)

    reg = Counter((e.get("subscription") or "not checked (no SSH)") for e in estate)
    ver = Counter(((e.get("os_version") or "?").split(".")[0]) for e in estate)
    addons = Counter(a.split()[0] for e in estate for a in e.get("addons") or [])

    rhel = {
        "total": len(estate),
        "physical": {"hosts": len(phys), "sockets": sum(e.get("sockets") or 0 for e in phys),
                     "unknown_socket_hosts": phys_unknown, "socket_pair_subs": phys_subs},
        "vmware": {"vms": len(vmw), "per_guest_subs": ceil_div(len(vmw), RULES["rhel_guests_per_sub"]),
                   "vdc_clusters": vdc, "vdc_subs": sum(x["vdc_subs"] for x in vdc)},
        "rhv": {"vms": len(rhvl), "per_guest_subs": ceil_div(len(rhvl), RULES["rhel_guests_per_sub"]),
                "vdc_clusters": rhv_vdc, "vdc_subs": sum(x["vdc_subs"] for x in rhv_vdc),
                "hypervisor_hosts": len(rhv_hosts), "hypervisor_sockets": sum(h.get("sockets") or 0 for h in rhv_hosts),
                "hypervisor_socket_pair_subs": rhv_hyp_subs,
                "rhvh_hosts": sum(1 for h in rhv_hosts if h.get("type") == "RHV-H"),
                "rhelh_hosts": sum(1 for h in rhv_hosts if h.get("type") == "RHEL-H")},
        "other_virtual": {"vms": len(other_virt), "per_guest_subs": ceil_div(len(other_virt), RULES["rhel_guests_per_sub"]),
                          "platforms": dict(Counter(e.get("platform") for e in other_virt))},
        "openshift_virtualization": {"vms": len(ocpv)},
        "powered_off_excluded": powered_off,
        "by_major_version": dict(sorted(ver.items())),
        "registration": dict(reg), "addons": dict(addons),
        "estate": estate,
    }
    totals = {
        "ocp_core_pair_subs": sum(c["chargeable"]["core_pair_subs"] for c in ocp),
        "ocp_bm_socket_pair_subs": sum(c["chargeable"]["bm_socket_pair_subs"] for c in ocp),
        "ocp_unknown_nodes": sum(c["chargeable"]["unknown_nodes"] for c in ocp),
        "odf_core_pair_subs": sum(c["odf"]["core_pair_subs"] for c in ocp),
        "rhel_physical_subs": phys_subs,
        "rhel_vmware_per_guest_subs": rhel["vmware"]["per_guest_subs"],
        "rhel_vmware_vdc_subs": rhel["vmware"]["vdc_subs"],
        "rhel_rhv_per_guest_subs": rhel["rhv"]["per_guest_subs"],
        "rhel_rhv_vdc_subs": rhel["rhv"]["vdc_subs"],
        "rhv_hypervisor_socket_pair_subs": rhv_hyp_subs,
        "rhel_other_virtual_subs": rhel["other_virtual"]["per_guest_subs"],
        "ocpv_rhel_vms": len(ocpv),
    }
    notes = [
        "Estimates only - validate against the customer's Red Hat subscription agreement and current product terms.",
        "OpenShift: control-plane and infra-labelled nodes are excluded; nodes with the worker role are counted.",
        "OpenShift on VMs: 1 core-pair subscription = 2 cores = 4 vCPU (SMT). Bare metal: 1 socket-pair = up to 2 sockets / 64 cores.",
        "RHEL guests: per-guest model assumes 1 subscription covers 2 VMs; RHEL for Virtual Datacenters covers unlimited guests per 2-socket host.",
        "vCenter/RHV VMs whose name matches an OpenShift node, or that report RHCOS, are excluded from RHEL counts.",
        "RHV: hypervisor hosts sized in socket-pairs; the HostedEngine VM is not counted as a RHEL guest. "
        "RHEL guests on RHV are shown both per-guest and as RHEL for Virtual Datacenters per RHV cluster "
        "(some RHV bundles already include RHEL VDC guest rights - check the contract).",
        "RHEL VMs on OpenShift Virtualization are listed separately - guest entitlement depends on the OpenShift offering purchased.",
    ]
    if totals["ocp_unknown_nodes"]:
        notes.append(f"{totals['ocp_unknown_nodes']} OpenShift worker node(s) could not be classified as physical/virtual "
                     "(platform 'None'); add vCenter or node SSH discovery to resolve.")
    bm_unk = sum(c["chargeable"]["bm_unknown_socket_nodes"] for c in ocp)
    if bm_unk:
        notes.append(f"{bm_unk} bare-metal OpenShift worker(s) had unknown socket count; {RULES['assume_sockets_if_unknown']} sockets assumed "
                     "(enable node SSH enrichment for exact sockets/cores).")
    if phys_unknown:
        notes.append(f"{phys_unknown} physical RHEL host(s) had unknown socket count; {RULES['assume_sockets_if_unknown']} sockets assumed.")
    if powered_off:
        notes.append(f"{powered_off} powered-off RHEL VM(s) excluded from counts.")
    return {"generated_at": now_iso(), "rules": RULES, "totals": totals, "openshift": ocp, "rhel": rhel, "notes": notes}
