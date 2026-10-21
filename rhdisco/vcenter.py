"""VMware vCenter discovery via the vSphere API (pyVmomi). Read-only; a vCenter 'Read-only' role is sufficient.

Collects ESXi hosts (sockets/cores/memory/NICs - needed for RHEL for Virtual Datacenters sizing)
and every VM with its guest OS, vCPU/memory/disk/NIC configuration. Uses the PropertyCollector so
large inventories (thousands of VMs) are fetched in a handful of round-trips.
"""
import ssl

from .util import Logger, classify_os, now_iso

HOST_PROPS = [
    "name", "parent", "summary.hardware", "summary.config.product.fullName", "runtime.connectionState",
    "runtime.powerState", "runtime.inMaintenanceMode", "config.network.pnic", "vm",
]
VM_PROPS = [
    "name", "config.template", "config.guestFullName", "config.guestId", "config.uuid", "config.instanceUuid",
    "config.hardware.numCPU", "config.hardware.numCoresPerSocket", "config.hardware.memoryMB",
    "config.hardware.device", "config.version", "runtime.powerState", "runtime.host",
    "guest.guestFullName", "guest.guestId", "guest.hostName", "guest.ipAddress", "guest.toolsRunningStatus",
    "guest.toolsVersionStatus2", "guest.net", "summary.storage.committed", "resourcePool", "parent",
]


def _collect(si, vimtype, props, log):
    from pyVmomi import vmodl
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        ts = vmodl.query.PropertyCollector.TraversalSpec(name="tv", path="view", skip=False, type=view.__class__)
        os_ = vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[ts])
        ps = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=props, all=False)
        fs = vmodl.query.PropertyCollector.FilterSpec(objectSet=[os_], propSet=[ps])
        pc = content.propertyCollector
        r = pc.RetrievePropertiesEx([fs], vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=500))
        out = []
        while r:
            for o in r.objects:
                d = {"_obj": o.obj, "_moid": o.obj._moId}
                for p in o.propSet or []:
                    d[p.name] = p.val
                out.append(d)
            r = pc.ContinueRetrievePropertiesEx(r.token) if r.token else None
        return out
    finally:
        view.Destroy()


class _Names:
    """Caches managed-object names and their cluster/datacenter ancestry."""

    def __init__(self):
        self.cache = {}

    def name(self, mo):
        if mo is None:
            return ""
        k = mo._moId
        if k not in self.cache:
            try:
                self.cache[k] = mo.name
            except Exception:
                self.cache[k] = k
        return self.cache[k]

    def datacenter(self, mo):
        from pyVmomi import vim
        k = "dc:" + mo._moId
        if k not in self.cache:
            p, dc = mo, ""
            try:
                while p is not None:
                    if isinstance(p, vim.Datacenter):
                        dc = p.name
                        break
                    p = p.parent
            except Exception:
                pass
            self.cache[k] = dc
        return self.cache[k]


def discover_vcenter(params, log=None):
    """params: host, port, username, password, verify_tls, name"""
    log = log or Logger()
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim

    verify = params.get("verify_tls") in (True, "true", "1")
    kw = dict(host=params["host"], user=params["username"], pwd=params["password"], port=int(params.get("port") or 443))
    if not verify:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kw["sslContext"] = ctx
    si = SmartConnect(**kw)
    try:
        about = si.content.about
        res = {"type": "vcenter", "host": params["host"], "name": params.get("name") or params["host"],
               "discovered_at": now_iso(), "version": f"{about.fullName}", "api_version": about.apiVersion,
               "instance_uuid": about.instanceUuid}
        log(f"Connected to {about.fullName}")
        names = _Names()

        hosts_raw = _collect(si, vim.HostSystem, HOST_PROPS, log)
        hosts, host_by_moid = [], {}
        for h in hosts_raw:
            hw = h.get("summary.hardware")
            parent = h.get("parent")
            is_cluster = isinstance(parent, vim.ClusterComputeResource)
            pnics = []
            for p in h.get("config.network.pnic") or []:
                ls = getattr(p, "linkSpeed", None)
                pnics.append({"name": p.device, "mac": p.mac, "driver": p.driver,
                              "speed_mbps": ls.speedMb if ls else None, "state": "up" if ls else "down", "physical": True})
            rec = {
                "moid": h["_moid"], "name": h.get("name"),
                "cluster": names.name(parent) if is_cluster else "(standalone)",
                "datacenter": names.datacenter(h["_obj"]),
                "esxi_version": h.get("summary.config.product.fullName", ""),
                "vendor": getattr(hw, "vendor", ""), "model": getattr(hw, "model", ""),
                "cpu_model": getattr(hw, "cpuModel", ""), "sockets": getattr(hw, "numCpuPkgs", None),
                "cores": getattr(hw, "numCpuCores", None), "threads": getattr(hw, "numCpuThreads", None),
                "cpu_mhz": getattr(hw, "cpuMhz", None), "memory_bytes": getattr(hw, "memorySize", 0),
                "connection_state": str(h.get("runtime.connectionState", "")), "power_state": str(h.get("runtime.powerState", "")),
                "maintenance": bool(h.get("runtime.inMaintenanceMode")), "nics": pnics, "vm_count": len(h.get("vm") or []),
            }
            hosts.append(rec)
            host_by_moid[h["_moid"]] = rec
        log(f"{len(hosts)} ESXi host(s)")

        vms_raw = _collect(si, vim.VirtualMachine, VM_PROPS, log)
        vms, templates = [], 0
        for v in vms_raw:
            if v.get("config.template"):
                templates += 1
                continue
            disks, nics = [], []
            for d in v.get("config.hardware.device") or []:
                if isinstance(d, vim.vm.device.VirtualDisk):
                    fn = getattr(d.backing, "fileName", "") or ""
                    disks.append({"name": d.deviceInfo.label if d.deviceInfo else "", "size_bytes": d.capacityInBytes,
                                  "datastore": fn.split("]")[0].lstrip("[") if "]" in fn else "",
                                  "thin": bool(getattr(d.backing, "thinProvisioned", False))})
                elif isinstance(d, vim.vm.device.VirtualEthernetCard):
                    b = d.backing
                    net = getattr(b, "deviceName", "") or (getattr(getattr(b, "port", None), "portgroupKey", "") if hasattr(b, "port") else "")
                    nics.append({"name": d.deviceInfo.label if d.deviceInfo else "", "model": type(d).__name__.replace("vim.vm.device.", "").replace("Virtual", ""),
                                 "mac": getattr(d, "macAddress", ""), "network": net,
                                 "connected": bool(d.connectable and d.connectable.connected)})
            ips = {}
            for g in v.get("guest.net") or []:
                ips[g.macAddress] = ",".join(a for a in (g.ipAddress or []) if ":" not in a)
            for n in nics:
                n["ipv4"] = ips.get(n["mac"], "")
            g_full, c_full = v.get("guest.guestFullName") or "", v.get("config.guestFullName") or ""
            g_id, c_id = v.get("guest.guestId") or "", v.get("config.guestId") or ""
            os_text, os_id = (g_full, g_id) if g_full else (c_full, c_id)
            fam, ver = classify_os(os_text, os_id)
            hmo = v.get("runtime.host")
            hrec = host_by_moid.get(hmo._moId) if hmo is not None else None
            ncpu = v.get("config.hardware.numCPU") or 0
            cps = v.get("config.hardware.numCoresPerSocket") or 1
            vms.append({
                "moid": v["_moid"], "name": v.get("name"), "power_state": str(v.get("runtime.powerState", "")),
                "os_family": fam, "os_version": ver or "", "os_name": os_text,
                "os_source": "vmware-tools" if g_full else "vm-config",
                "guest_id_config": c_id, "guest_os_config": c_full, "guest_os_tools": g_full,
                "hostname": v.get("guest.hostName") or "", "ip": v.get("guest.ipAddress") or "",
                "vcpu": ncpu, "cores_per_socket": cps, "sockets": max(1, ncpu // cps) if ncpu else None,
                "memory_bytes": (v.get("config.hardware.memoryMB") or 0) * 1024 ** 2,
                "disks": disks, "disk_total_bytes": sum(d["size_bytes"] or 0 for d in disks),
                "storage_committed_bytes": v.get("summary.storage.committed") or 0, "nics": nics,
                "esxi_host": hrec["name"] if hrec else "", "cluster": hrec["cluster"] if hrec else "",
                "datacenter": hrec["datacenter"] if hrec else "",
                "tools_status": str(v.get("guest.toolsRunningStatus") or ""), "tools_version": str(v.get("guest.toolsVersionStatus2") or ""),
                "hw_version": v.get("config.version") or "", "uuid": v.get("config.uuid") or "",
                "config_mismatch": bool(g_full and c_full and classify_os(g_full)[0] != classify_os(c_full, c_id)[0]),
            })
        res["hosts"] = hosts
        res["vms"] = vms
        res["template_count"] = templates
        rhel = [x for x in vms if x["os_family"] == "RHEL"]
        log(f"{len(vms)} VM(s) ({templates} templates skipped); {len(rhel)} RHEL, "
            f"{sum(1 for x in vms if x['os_family'] == 'RHCOS')} RHCOS (OpenShift nodes)")
        clusters = {}
        for h in hosts:
            c = clusters.setdefault((h["datacenter"], h["cluster"]), {"datacenter": h["datacenter"], "cluster": h["cluster"],
                                                                       "hosts": 0, "sockets": 0, "cores": 0, "memory_bytes": 0})
            c["hosts"] += 1
            c["sockets"] += h["sockets"] or 0
            c["cores"] += h["cores"] or 0
            c["memory_bytes"] += h["memory_bytes"] or 0
        res["clusters"] = list(clusters.values())
        return res
    finally:
        Disconnect(si)
