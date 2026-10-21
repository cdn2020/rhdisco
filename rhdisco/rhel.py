"""RHEL / Linux discovery over SSH (paramiko).

Runs one read-only shell script per host and parses the output into a structured record:
OS release, CPU topology (sockets / cores / threads), memory, physical disks, NICs (speed, MAC, IP),
hardware vendor/model, virtualisation type, and Red Hat subscription / registration status.
Nothing is written to the target host.
"""
import io
import re
import shlex
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

from .util import Logger, classify_os, now_iso

DISCOVERY_SCRIPT = r'''
export LC_ALL=C PATH=$PATH:/usr/sbin:/sbin
S(){ echo "@@@$1"; }
SUDO=""
if [ "$(id -u)" != "0" ] && [ "${RHD_SUDO:-1}" = "1" ] && sudo -n true 2>/dev/null; then SUDO="sudo -n"; fi
S whoami; echo "$(id -un) uid=$(id -u) sudo=$([ -n "$SUDO" ] && echo yes || echo no)"
S hostname; hostname -f 2>/dev/null || hostname
S os_release; cat /etc/os-release 2>/dev/null
S redhat_release; cat /etc/redhat-release 2>/dev/null
S kernel; uname -r
S arch; uname -m
S lscpu; lscpu 2>/dev/null
S cpuinfo_sockets; grep -c '^processor' /proc/cpuinfo; grep 'physical id' /proc/cpuinfo | sort -u | wc -l
S meminfo; grep -E '^(MemTotal|SwapTotal):' /proc/meminfo
S virt; (systemd-detect-virt 2>/dev/null || echo none) | head -1
S dmi
for f in sys_vendor product_name product_serial product_uuid bios_vendor bios_version chassis_type; do
  v=$(cat /sys/class/dmi/id/$f 2>/dev/null)
  [ -z "$v" ] && [ -n "$SUDO" ] && v=$($SUDO cat /sys/class/dmi/id/$f 2>/dev/null)
  echo "$f=$v"
done
S lsblk
lsblk -b -d -n -P -o NAME,SIZE,TYPE,MODEL,ROTA,TRAN,VENDOR,SERIAL 2>/dev/null || lsblk -b -d -n -P -o NAME,SIZE,TYPE,MODEL,ROTA 2>/dev/null
S fs; df -P -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | tail -n +2
S nics
for n in /sys/class/net/*; do
  i=$(basename "$n"); [ "$i" = "lo" ] && continue
  drv=""; [ -e "$n/device/driver" ] && drv=$(basename "$(readlink "$n/device/driver")")
  phys=0; [ -e "$n/device" ] && phys=1
  bond=""; [ -d "$n/bonding" ] && bond="bond"; [ -e "$n/master" ] && bond="slave:$(basename "$(readlink "$n/master")")"
  ip4=$(ip -4 -o addr show dev "$i" 2>/dev/null | awk '{print $4}' | paste -sd, -)
  echo "name=$i|mac=$(cat $n/address 2>/dev/null)|state=$(cat $n/operstate 2>/dev/null)|speed=$(cat $n/speed 2>/dev/null)|mtu=$(cat $n/mtu 2>/dev/null)|driver=$drv|physical=$phys|bond=$bond|ip=$ip4"
done
S pci_nics; (lspci 2>/dev/null | grep -iE 'ethernet|network|fibre channel|infiniband') | head -20
S sm_status; (timeout 25 $SUDO subscription-manager status 2>&1 || true) | head -25
S sm_identity; (timeout 25 $SUDO subscription-manager identity 2>&1 || true) | head -10
S sm_consumed; (timeout 25 $SUDO subscription-manager list --consumed 2>&1 || true) | grep -E '^(Subscription Name|SKU|Contract|Service Level|Quantity Used|Ends):' | head -60
S rhsm_server; grep -E '^\s*hostname\s*=' /etc/rhsm/rhsm.conf 2>/dev/null | head -1
S insights; ( [ -f /etc/insights-client/.registered ] && echo registered ) || ( [ -f /etc/insights-client/.unregistered ] && echo unregistered ) || echo unknown
S addons; rpm -q --qf '%{NAME} %{VERSION}\n' pacemaker corosync resource-agents-sap resource-agents-sap-hana kernel-rt rhel-system-roles-sap ansible-core satellite foreman katello-ca-consumer-$(hostname -f 2>/dev/null) 2>/dev/null | grep -v 'not installed'
S uptime; cut -d' ' -f1 /proc/uptime
S end
'''

SSH_ERRORS = (socket.timeout, socket.error, EOFError)


def _load_pkey(key_text, passphrase=None):
    import paramiko
    errors = []
    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_text), password=passphrase or None)
        except Exception as e:  # try next key type
            errors.append(f"{cls.__name__}: {e}")
    raise ValueError("Unable to load private key (" + "; ".join(errors) + ")")


def parse_host_line(line, default_user, default_port=22):
    """Accept 'host', 'host:port', 'user@host', 'user@host:port', optionally followed by a label after whitespace/comma."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    parts = re.split(r"[\s,;]+", line, maxsplit=1)
    target = parts[0]
    label = parts[1].strip() if len(parts) > 1 else ""
    user = default_user
    if "@" in target:
        user, target = target.split("@", 1)
    port = default_port
    if target.startswith("[") and "]" in target:  # [ipv6]:port
        host, rest = target[1:].split("]", 1)
        if rest.startswith(":"):
            port = int(rest[1:])
    elif target.count(":") == 1:
        host, p = target.split(":")
        port = int(p)
    else:
        host = target
    return {"host": host, "port": int(port), "user": user, "label": label}


def run_ssh_script(host, port, user, password=None, key_text=None, passphrase=None, timeout=20,
                   use_sudo=True, jump=None, script=DISCOVERY_SCRIPT, cmd_timeout=180):
    import paramiko
    pkey = _load_pkey(key_text, passphrase) if key_text else None
    sock = None
    jclient = None
    if jump and jump.get("host"):
        jclient = paramiko.SSHClient()
        jclient.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jkey = _load_pkey(jump["key"], jump.get("passphrase")) if jump.get("key") else pkey
        jclient.connect(jump["host"], port=int(jump.get("port") or 22), username=jump.get("user") or user,
                        password=jump.get("password") or password, pkey=jkey, timeout=timeout,
                        allow_agent=False, look_for_keys=False, banner_timeout=timeout, auth_timeout=timeout)
        sock = jclient.get_transport().open_channel("direct-tcpip", (host, int(port)), ("127.0.0.1", 0))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=int(port), username=user, password=password or None, pkey=pkey, timeout=timeout,
                       allow_agent=False, look_for_keys=False, sock=sock, banner_timeout=timeout, auth_timeout=timeout)
        env = "RHD_SUDO=1" if use_sudo else "RHD_SUDO=0"
        stdin, stdout, stderr = client.exec_command(f"{env} bash -s", timeout=cmd_timeout)
        stdin.write(script)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        return out
    finally:
        client.close()
        if jclient:
            jclient.close()


def _sections(out):
    secs, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@@"):
            cur = line[3:].strip()
            secs[cur] = []
        elif cur:
            secs[cur].append(line)
    return secs


def _kv(lines, sep="="):
    d = {}
    for l in lines:
        if sep in l:
            k, v = l.split(sep, 1)
            d[k.strip()] = v.strip().strip('"')
    return d


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse_discovery_output(out):
    s = _sections(out)
    rec = {"raw_complete": "end" in s}
    who = (s.get("whoami") or [""])[0]
    rec["ssh_user_info"] = who
    rec["sudo_used"] = "sudo=yes" in who or "uid=0" in who
    rec["hostname"] = (s.get("hostname") or [""])[0].strip()
    osr = _kv(s.get("os_release", []))
    rec["os_name"] = osr.get("PRETTY_NAME") or (s.get("redhat_release") or [""])[0]
    rec["os_id"] = osr.get("ID", "")
    rec["os_version"] = osr.get("VERSION_ID", "")
    rec["redhat_release"] = " ".join(s.get("redhat_release", [])).strip()
    fam, _ = classify_os(f"{rec['os_name']} {rec['redhat_release']}", rec["os_id"])
    if rec["os_id"] == "rhcos":
        fam = "RHCOS"
    rec["os_family"] = fam
    rec["kernel"] = (s.get("kernel") or [""])[0]
    rec["arch"] = (s.get("arch") or [""])[0]

    # CPU topology
    lc = _kv(s.get("lscpu", []), ":")
    logical = _int(lc.get("CPU(s)"))
    sockets = _int(lc.get("Socket(s)"))
    cps = _int(lc.get("Core(s) per socket"))
    tpc = _int(lc.get("Thread(s) per core"), 1)
    ci = s.get("cpuinfo_sockets", [])
    if logical is None and ci:
        logical = _int(ci[0])
    if sockets is None and len(ci) > 1:
        sockets = _int(ci[1]) or None
    if (not cps) and sockets and logical:
        cps = max(1, logical // (sockets * (tpc or 1)))
    rec["cpu"] = {
        "model": lc.get("Model name", ""),
        "vendor": lc.get("Vendor ID", ""),
        "sockets": sockets,
        "cores_per_socket": cps,
        "threads_per_core": tpc,
        "physical_cores": (sockets * cps) if sockets and cps else None,
        "logical_cpus": logical,
        "mhz": lc.get("CPU MHz") or lc.get("CPU max MHz", ""),
        "hypervisor_vendor": lc.get("Hypervisor vendor", ""),
    }
    mem = _kv(s.get("meminfo", []), ":")
    rec["memory_bytes"] = (_int(mem.get("MemTotal", "0").split()[0], 0) or 0) * 1024
    rec["swap_bytes"] = (_int(mem.get("SwapTotal", "0").split()[0], 0) or 0) * 1024

    virt = ((s.get("virt") or ["none"])[0] or "none").strip()
    dmi = _kv(s.get("dmi", []))
    rec["hardware"] = {
        "vendor": dmi.get("sys_vendor", ""),
        "model": dmi.get("product_name", ""),
        "serial": dmi.get("product_serial", ""),
        "uuid": dmi.get("product_uuid", ""),
        "bios": f"{dmi.get('bios_vendor', '')} {dmi.get('bios_version', '')}".strip(),
    }
    hv_hint = f"{dmi.get('sys_vendor', '')} {dmi.get('product_name', '')}".lower()
    if virt in ("", "none", "unknown"):
        if any(x in hv_hint for x in ("vmware", "virtualbox", "kvm", "qemu", "openstack", "hvm domu", "virtual machine", "amazon ec2", "google compute", "red hat")):
            virt = "vm"  # DMI says virtual even though systemd-detect-virt was unavailable
        elif lc.get("Hypervisor vendor"):
            virt = lc["Hypervisor vendor"].lower()
    rec["virtualization"] = virt if virt not in ("", "unknown") else "none"
    rec["is_virtual"] = rec["virtualization"] not in ("none",)

    disks = []
    for l in s.get("lsblk", []):
        d = dict(re.findall(r'(\w+)="([^"]*)"', l))
        if not d or d.get("TYPE") not in ("disk", "mpath"):
            continue
        if d.get("NAME", "").startswith(("loop", "zram", "sr", "ram")):
            continue
        disks.append({
            "name": d.get("NAME"), "size_bytes": _int(d.get("SIZE"), 0), "type": d.get("TYPE"),
            "model": (d.get("MODEL") or "").strip(), "vendor": (d.get("VENDOR") or "").strip(),
            "rotational": d.get("ROTA") == "1", "transport": d.get("TRAN", ""), "serial": d.get("SERIAL", ""),
        })
    rec["disks"] = disks
    rec["disk_total_bytes"] = sum(d["size_bytes"] or 0 for d in disks)
    fss = []
    for l in s.get("fs", []):
        p = l.split()
        if len(p) >= 6:
            fss.append({"fs": p[0], "size_bytes": _int(p[1], 0), "used_bytes": _int(p[2], 0), "mount": p[5]})
    rec["filesystems"] = fss

    nics = []
    for l in s.get("nics", []):
        d = dict(x.split("=", 1) for x in l.split("|") if "=" in x)
        if not d.get("name"):
            continue
        name = d["name"]
        if re.match(r"^(veth|cali|tun|tap|virbr|docker|cni|flannel|genev|ovs-system|br-int|br-ex|ovn|vxlan|vnet|kube-ipvs|ifb|dummy|virbr)", name):
            continue
        spd = _int(d.get("speed"))
        nics.append({
            "name": name, "mac": d.get("mac", ""), "state": d.get("state", ""),
            "speed_mbps": spd if spd and spd > 0 else None, "mtu": _int(d.get("mtu")),
            "driver": d.get("driver", ""), "physical": d.get("physical") == "1", "bond": d.get("bond", ""),
            "ipv4": d.get("ip", ""),
        })
    rec["nics"] = nics
    rec["pci_network_devices"] = [l.strip() for l in s.get("pci_nics", []) if l.strip()]

    # Subscription
    sm_status = "\n".join(s.get("sm_status", []))
    sm = {"raw_status": sm_status.strip()[:600]}
    m = re.search(r"Overall Status:\s*(.+)", sm_status)
    sm["overall_status"] = m.group(1).strip() if m else ""
    m = re.search(r"Content Access Mode is set to (.+?)\.", sm_status)
    sm["sca"] = bool(m and "simple content access" in m.group(1).lower()) or "simple content access" in sm_status.lower()
    if "requires root" in sm_status.lower() or "must be root" in sm_status.lower():
        sm["overall_status"] = "unknown (needs root/sudo)"
    elif "command not found" in sm_status.lower() or "no such file" in sm_status.lower():
        sm["overall_status"] = "subscription-manager not installed"
    elif "not registered" in sm_status.lower() or "unknown" == sm["overall_status"].lower():
        sm["overall_status"] = sm["overall_status"] or "Not registered"
    ident = _kv(s.get("sm_identity", []), ":")
    sm["system_identity"] = ident.get("system identity", "")
    sm["org"] = ident.get("org name", "")
    srv = _kv(s.get("rhsm_server", []))
    sm["rhsm_server"] = srv.get("hostname", "")
    sm["registered_to"] = ("Red Hat CDN" if "subscription.rhsm.redhat.com" in sm["rhsm_server"] else
                           ("Satellite: " + sm["rhsm_server"]) if sm["rhsm_server"] else "")
    consumed, cur = [], {}
    for l in s.get("sm_consumed", []):
        if ":" not in l:
            continue
        k, v = [x.strip() for x in l.split(":", 1)]
        if k == "Subscription Name" and cur:
            consumed.append(cur)
            cur = {}
        cur[k] = v
    if cur:
        consumed.append(cur)
    sm["consumed"] = consumed
    rec["subscription"] = sm
    rec["insights"] = (s.get("insights") or ["unknown"])[0]
    rec["addons"] = [l.strip() for l in s.get("addons", []) if l.strip()]
    rec["uptime_days"] = round(float((s.get("uptime") or ["0"])[0] or 0) / 86400, 1) if (s.get("uptime") or [""])[0] else None
    return rec


def discover_host(target, creds, log=None, jump=None):
    log = log or Logger()
    base = {"target": target["host"], "port": target["port"], "label": target.get("label", ""),
            "ssh_user": target["user"], "discovered_at": now_iso()}
    try:
        out = run_ssh_script(target["host"], target["port"], target["user"], password=creds.get("password"),
                             key_text=creds.get("key"), passphrase=creds.get("passphrase"),
                             timeout=int(creds.get("timeout") or 20), use_sudo=creds.get("sudo", True), jump=jump)
        rec = parse_discovery_output(out)
        base.update(rec)
        base["status"] = "ok" if rec.get("raw_complete") else "partial"
        log(f"{target['host']}: {rec.get('os_name')} | {rec['cpu'].get('sockets')} socket(s) / "
            f"{rec['cpu'].get('logical_cpus')} CPU | {round(rec['memory_bytes'] / 1024 ** 3, 1)} GiB | {rec['virtualization']}")
    except Exception as e:  # keep going with other hosts
        base["status"] = "error"
        base["error"] = f"{type(e).__name__}: {e}"
        log.error(f"{target['host']}: {base['error']}")
    return base


def discover_rhel(params, log=None):
    """params: hosts (text or list), username, password, key, passphrase, sudo, concurrency, jump{}"""
    log = log or Logger()
    hosts = params.get("hosts") or ""
    if isinstance(hosts, str):
        hosts = hosts.splitlines()
    user = params.get("username") or "root"
    targets = [t for t in (parse_host_line(h, user, int(params.get("port") or 22)) for h in hosts) if t]
    if not targets:
        raise ValueError("No hosts supplied")
    creds = {k: params.get(k) for k in ("password", "key", "passphrase", "timeout")}
    creds["sudo"] = params.get("sudo", True) not in (False, "false", "0", 0)
    jump = params.get("jump") if (params.get("jump") or {}).get("host") else None
    conc = max(1, min(int(params.get("concurrency") or 10), 64))
    log(f"SSH discovery of {len(targets)} host(s), concurrency {conc}")
    results = []
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(discover_host, t, creds, log, jump): t for t in targets}
        for f in as_completed(futs):
            results.append(f.result())
    order = {t["host"]: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["target"], 0))
    ok = sum(1 for r in results if r["status"] != "error")
    log(f"SSH discovery complete: {ok}/{len(results)} reachable")
    return {"type": "rhel", "discovered_at": now_iso(), "hosts": results}
