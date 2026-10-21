"""Red Hat Virtualization (RHV / oVirt 4.x) discovery via the RHV Manager REST API (v4).

Read-only. A Manager user with the built-in 'ReadOnlyAdmin' role (system-wide) is sufficient,
e.g. 'discovery@internal' or an LDAP user such as 'svc-ro@corp.example.com'.

Collects: Manager version, data centers, clusters, hypervisor hosts (sockets/cores/threads, memory,
NICs, vendor/model/serial, RHEL-H vs RHV-H) and every VM with guest OS (guest agent or configured OS
type), vCPU topology, memory, disks (provisioned/actual size, storage domain) and NICs (network, MAC, IPs).

Works with RHV 4.0-4.4: uses `follow=` (4.2+) to fetch VM disks/NICs in one request and falls back
to per-VM requests on older managers.
"""
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

from .util import Logger, classify_os, now_iso

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _i(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _b(v):
    return v is True or str(v).lower() == "true"


def _lst(data, key):
    """RHV JSON wraps collections as {"vm": [...]}; empty collections are {}."""
    if not data:
        return []
    v = data.get(key) if isinstance(data, dict) else data
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _ref(o, key):
    return ((o or {}).get(key) or {}).get("id")


class RHVClient:
    def __init__(self, url, username, password, verify=False, timeout=60, log=None):
        url = url.strip().rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        if "/ovirt-engine" not in url:
            url += "/ovirt-engine/api"
        elif not url.endswith("/api"):
            url = url.split("/ovirt-engine")[0] + "/ovirt-engine/api"
        self.api = url
        self.base = url.split("/ovirt-engine")[0]
        self.timeout = timeout
        self.log = log or Logger()
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers.update({"Accept": "application/json", "Version": "4"})
        token = None
        try:  # SSO token avoids a new engine session per request
            r = self.s.post(f"{self.base}/ovirt-engine/sso/oauth/token",
                            data={"grant_type": "password", "scope": "ovirt-app-api", "username": username, "password": password},
                            headers={"Accept": "application/json"}, timeout=timeout)
            if r.ok:
                token = r.json().get("access_token")
            elif r.status_code in (400, 401):
                msg = ""
                try:
                    msg = r.json().get("error_description") or r.json().get("error") or ""
                except ValueError:
                    pass
                raise PermissionError(f"RHV login failed: {msg or r.status_code}. Use user@profile, e.g. admin@internal")
        except requests.RequestException:
            token = None
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        else:
            self.s.auth = (username, password)
            self.s.headers["Prefer"] = "persistent-auth"

    def get(self, path, params=None, quiet=False):
        r = self.s.get(self.api + path, params=params, timeout=self.timeout)
        if r.status_code == 401:
            raise PermissionError("HTTP 401 Unauthorized - check username (user@profile) and password")
        if r.status_code in (403, 404):
            if not quiet:
                self.log.warn(f"{path}: HTTP {r.status_code}")
            return None
        r.raise_for_status()
        return r.json() if r.content else {}

    def close(self):
        try:
            if "Authorization" in self.s.headers:
                self.s.get(f"{self.base}/ovirt-engine/services/sso-logout", timeout=10)
        except Exception:
            pass


def _ips_from_reported(devs):
    out = {}
    for d in devs:
        mac = ((d.get("mac") or {}).get("address") or "").lower()
        ips = [ip.get("address") for ip in _lst(d.get("ips"), "ip") if ip.get("version", "v4") == "v4" and ip.get("address")]
        if ips:
            out[mac] = out.get(mac, []) + ips
    return out


def discover_rhv(params, log=None):
    """params: url (manager FQDN or API URL), username (user@profile), password, verify_tls, name, concurrency"""
    log = log or Logger()
    c = RHVClient(params["url"], params["username"], params.get("password", ""),
                  verify=params.get("verify_tls") in (True, "true", "1"), log=log)
    try:
        root = c.get("") or {}
        pi = root.get("product_info") or {}
        ver = (pi.get("version") or {}).get("full_version") or ""
        res = {"type": "rhv", "url": c.api, "name": params.get("name") or c.base.split("//")[-1].split(":")[0],
               "discovered_at": now_iso(), "product": pi.get("name", ""), "version": ver}
        log(f"Connected to {pi.get('name', 'RHV Manager')} {ver}")

        dcs = {d["id"]: d.get("name") for d in _lst(c.get("/datacenters"), "data_center")}
        clusters = {}
        for cl in _lst(c.get("/clusters"), "cluster"):
            v = cl.get("version") or {}
            clusters[cl["id"]] = {"name": cl.get("name"), "datacenter": dcs.get(_ref(cl, "data_center"), ""),
                                  "cpu_type": (cl.get("cpu") or {}).get("type", ""),
                                  "compat_version": f"{v.get('major', '')}.{v.get('minor', '')}" if v else ""}
        sds = {s["id"]: s.get("name") for s in _lst(c.get("/storagedomains", quiet=True), "storage_domain")}
        nets = {n["id"]: n.get("name") for n in _lst(c.get("/networks", quiet=True), "network")}
        vnps = {p["id"]: {"profile": p.get("name"), "network": nets.get(_ref(p, "network"), "")}
                for p in _lst(c.get("/vnicprofiles", quiet=True), "vnic_profile")}
        log(f"{len(dcs)} data center(s), {len(clusters)} cluster(s), {len(sds)} storage domain(s)")

        # ---------------------------------------------------------------- hosts
        hosts_raw = _lst(c.get("/hosts"), "host")
        conc = max(1, min(int(params.get("concurrency") or 10), 32))

        def host_rec(h):
            cpu = h.get("cpu") or {}
            topo = cpu.get("topology") or {}
            sockets, cps, tpc = _i(topo.get("sockets")), _i(topo.get("cores")), _i(topo.get("threads"), 1)
            hw = h.get("hardware_information") or {}
            os_ = h.get("os") or {}
            cl = clusters.get(_ref(h, "cluster"), {})
            nics = []
            try:
                for n in _lst(c.get(f"/hosts/{h['id']}/nics", quiet=True), "host_nic"):
                    spd = _i(n.get("speed"))
                    nics.append({"name": n.get("name"), "mac": (n.get("mac") or {}).get("address", ""),
                                 "speed_mbps": spd // 1_000_000 if spd else None, "state": n.get("status", ""),
                                 "ipv4": (n.get("ip") or {}).get("address", ""), "mtu": _i(n.get("mtu"), None),
                                 "bond": "bond" if n.get("bonding") else ("slave" if n.get("base_interface") else ""),
                                 "network": nets.get(_ref(n, "network"), ""), "physical": not n.get("bonding") and not n.get("vlan")})
            except Exception as e:
                log.warn(f"host {h.get('name')}: NICs unavailable ({e})")
            return {
                "id": h["id"], "name": h.get("name"), "address": h.get("address", ""),
                "cluster": cl.get("name", ""), "datacenter": cl.get("datacenter", ""),
                "status": h.get("status", ""), "type": "RHV-H" if h.get("type") == "ovirt_node" else ("RHEL-H" if h.get("type") == "rhel" else h.get("type", "")),
                "os": f"{os_.get('type', '')} {(os_.get('version') or {}).get('full_version', '')}".strip(),
                "vendor": hw.get("manufacturer", ""), "model": hw.get("product_name", ""), "serial": hw.get("serial_number", ""),
                "cpu_model": cpu.get("name", ""), "cpu_mhz": _i(cpu.get("speed"), None),
                "sockets": sockets or None, "cores_per_socket": cps or None, "threads_per_core": tpc,
                "cores": (sockets * cps) if sockets and cps else None,
                "threads": (sockets * cps * tpc) if sockets and cps else None,
                "memory_bytes": _i(h.get("memory")), "max_sched_memory_bytes": _i(h.get("max_scheduling_memory")),
                "vms_active": _i((h.get("summary") or {}).get("active")), "vms_total": _i((h.get("summary") or {}).get("total")),
                "hosted_engine": _b((h.get("hosted_engine") or {}).get("configured")),
                "spm": (h.get("spm") or {}).get("status", ""), "nics": nics,
            }

        with ThreadPoolExecutor(max_workers=conc) as ex:
            hosts = list(ex.map(host_rec, hosts_raw))
        host_by_id = {h["id"]: h for h in hosts}
        log(f"{len(hosts)} hypervisor host(s), {sum(h['sockets'] or 0 for h in hosts)} sockets")

        # ---------------------------------------------------------------- VMs
        disks = {}
        for d in _lst(c.get("/disks", quiet=True), "disk"):
            disks[d["id"]] = d
        follow_ok = True
        try:
            vms_raw = _lst(c.get("/vms", params={"follow": "disk_attachments,nics.reported_devices"}), "vm")
        except requests.HTTPError:
            follow_ok = False
            vms_raw = _lst(c.get("/vms"), "vm")
        if follow_ok and vms_raw and "disk_attachments" not in vms_raw[0] and "nics" not in vms_raw[0]:
            follow_ok = False  # pre-4.2 manager silently ignored follow=
        log(f"{len(vms_raw)} VM(s); fetching disks/NICs {'in bulk' if follow_ok else 'per VM (manager < 4.2)'}")

        def expand(vm):
            if follow_ok:
                da = _lst(vm.get("disk_attachments"), "disk_attachment")
                nics = _lst(vm.get("nics"), "nic")
                rep = [r for n in nics for r in _lst(n.get("reported_devices"), "reported_device")]
            else:
                da = _lst(c.get(f"/vms/{vm['id']}/diskattachments", quiet=True), "disk_attachment")
                nics = _lst(c.get(f"/vms/{vm['id']}/nics", quiet=True), "nic")
                rep = _lst(c.get(f"/vms/{vm['id']}/reporteddevices", quiet=True), "reported_device")
            return da, nics, rep

        def vm_rec(vm):
            try:
                da, nics_raw, rep = expand(vm)
            except Exception as e:
                log.warn(f"VM {vm.get('name')}: {e}")
                da, nics_raw, rep = [], [], []
            dlist = []
            for a in da:
                d = disks.get(_ref(a, "disk")) or a.get("disk") or {}
                sd = [sds.get(s.get("id"), s.get("id")) for s in _lst(d.get("storage_domains"), "storage_domain")]
                lun = (d.get("lun_storage") or {})
                size = _i(d.get("provisioned_size")) or _i(lun.get("size"))
                dlist.append({"name": d.get("alias") or d.get("name") or a.get("logical_name") or "", "size_bytes": size,
                              "actual_bytes": _i(d.get("actual_size")), "interface": a.get("interface", ""),
                              "bootable": _b(a.get("bootable")), "format": d.get("format", ""),
                              "storage_type": d.get("storage_type", ""), "storage_domain": ",".join(x for x in sd if x),
                              "thin": d.get("sparse") in (True, "true"), "shareable": _b(d.get("shareable")),
                              "active": _b(a.get("active", True))})
            ipmap = _ips_from_reported(rep)
            nlist = []
            for n in nics_raw:
                mac = ((n.get("mac") or {}).get("address") or "").lower()
                vp = vnps.get(_ref(n, "vnic_profile"), {})
                nlist.append({"name": n.get("name"), "model": n.get("interface", ""), "mac": mac,
                              "network": vp.get("network", ""), "profile": vp.get("profile", ""),
                              "plugged": _b(n.get("plugged")), "linked": _b(n.get("linked")),
                              "ipv4": ",".join(ipmap.get(mac, []))})
            gos = vm.get("guest_operating_system") or {}
            gver = (gos.get("version") or {}).get("full_version", "")
            os_type = (vm.get("os") or {}).get("type", "")
            if gos.get("distribution"):
                os_name, src = f"{gos.get('distribution')} {gver}".strip(), "guest-agent"
            else:
                os_name, src = os_type, "vm-config"
            fam, ver = classify_os(os_name, os_type)
            if src == "guest-agent" and gver and fam != "Windows":
                ver = gver
            cfg_fam = classify_os(os_type)[0]
            topo = (vm.get("cpu") or {}).get("topology") or {}
            s_, c_, t_ = _i(topo.get("sockets"), 1), _i(topo.get("cores"), 1), _i(topo.get("threads"), 1)
            cl = clusters.get(_ref(vm, "cluster"), {})
            h = host_by_id.get(_ref(vm, "host"), {})
            status = vm.get("status", "")
            all_ips = sorted({ip for v in ipmap.values() for ip in v})
            return {
                "id": vm["id"], "name": vm.get("name"), "status": status,
                "power_state": "poweredOn" if status in ("up", "powering_up", "reboot_in_progress", "migrating", "paused") else "poweredOff",
                "os_family": fam, "os_version": ver or "", "os_name": os_name, "os_source": src, "os_type_config": os_type,
                "config_mismatch": bool(src == "guest-agent" and cfg_fam not in ("Other", fam)),
                "kernel": (gos.get("kernel") or {}).get("version", {}).get("full_version", "") if isinstance(gos.get("kernel"), dict) else "",
                "hostname": vm.get("fqdn", ""), "ip": all_ips[0] if all_ips else "",
                "sockets": s_, "cores_per_socket": c_, "threads_per_core": t_, "vcpu": s_ * c_ * t_,
                "memory_bytes": _i(vm.get("memory")),
                "memory_guaranteed_bytes": _i((vm.get("memory_policy") or {}).get("guaranteed")),
                "disks": dlist, "disk_total_bytes": sum(d["size_bytes"] for d in dlist),
                "disk_actual_bytes": sum(d["actual_bytes"] for d in dlist), "nics": nlist,
                "cluster": cl.get("name", ""), "datacenter": cl.get("datacenter", ""), "host": h.get("name", ""),
                "vm_type": vm.get("type", ""), "origin": vm.get("origin", ""),
                "hosted_engine": vm.get("origin") == "managed_hosted_engine" or vm.get("name") == "HostedEngine",
                "description": vm.get("description", ""), "created": vm.get("creation_time", ""),
            }

        with ThreadPoolExecutor(max_workers=conc if not follow_ok else 4) as ex:
            vms = list(ex.map(vm_rec, vms_raw))

        cl_summary = {}
        for h in hosts:
            k = (h["datacenter"], h["cluster"])
            s = cl_summary.setdefault(k, {"datacenter": h["datacenter"], "cluster": h["cluster"], "hosts": 0, "sockets": 0,
                                          "cores": 0, "memory_bytes": 0})
            s["hosts"] += 1
            s["sockets"] += h["sockets"] or 0
            s["cores"] += h["cores"] or 0
            s["memory_bytes"] += h["memory_bytes"] or 0
        for cid, cl in clusters.items():
            cl_summary.setdefault((cl["datacenter"], cl["name"]), {"datacenter": cl["datacenter"], "cluster": cl["name"], "hosts": 0,
                                                                   "sockets": 0, "cores": 0, "memory_bytes": 0})
        for (dc, name), s in cl_summary.items():
            meta = next((x for x in clusters.values() if x["name"] == name and x["datacenter"] == dc), {})
            s.update({"cpu_type": meta.get("cpu_type", ""), "compat_version": meta.get("compat_version", ""),
                      "vms": sum(1 for v in vms if v["cluster"] == name and v["datacenter"] == dc)})
        res.update({"hosts": hosts, "vms": vms, "clusters": list(cl_summary.values())})
        rhel = sum(1 for v in vms if v["os_family"] == "RHEL" and not v["hosted_engine"])
        log(f"{len(vms)} VM(s): {rhel} RHEL, {sum(1 for v in vms if v['os_family'] == 'Windows')} Windows, "
            f"{sum(1 for v in vms if v['power_state'] == 'poweredOn')} running")
        return res
    finally:
        c.close()
