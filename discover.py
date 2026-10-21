#!/usr/bin/env python3
"""Headless discovery for jump hosts without a browser.

    python3 discover.py --config targets.json --out assessment.json --xlsx assessment.xlsx

Secrets in the config may be given as "env:VAR_NAME" or "file:/path/to/file" instead of literal values.
The resulting assessment.json can be opened in the web UI (Load JSON) for review.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhdisco import __version__  # noqa: E402
from rhdisco.licensing import summarize  # noqa: E402
from rhdisco.util import Logger, now_iso  # noqa: E402


def resolve(v):
    if isinstance(v, dict):
        return {k: resolve(x) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x) for x in v]
    if isinstance(v, str) and v.startswith("env:"):
        return os.environ.get(v[4:], "")
    if isinstance(v, str) and v.startswith("file:"):
        with open(os.path.expanduser(v[5:])) as f:
            return f.read()
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="assessment.json")
    ap.add_argument("--xlsx")
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = resolve(json.load(f))
    log = Logger(lambda l: print(f"[{l['t']}] {l['level'].upper():5} {l['msg']}", flush=True))
    out = {"meta": {**(cfg.get("meta") or {}), "tool_version": __version__, "created": now_iso()},
           "openshift": [], "vcenter": [], "rhv": [], "rhel": []}
    for c in cfg.get("openshift", []):
        from rhdisco.openshift import discover_openshift
        try:
            out["openshift"].append(discover_openshift(c, log))
        except Exception as e:
            log.error(f"OpenShift {c.get('api_url')}: {e}")
    for c in cfg.get("vcenter", []):
        from rhdisco.vcenter import discover_vcenter
        try:
            out["vcenter"].append(discover_vcenter(c, log))
        except Exception as e:
            log.error(f"vCenter {c.get('host')}: {e}")
    for c in cfg.get("rhv", []):
        from rhdisco.rhv import discover_rhv
        try:
            out["rhv"].append(discover_rhv(c, log))
        except Exception as e:
            log.error(f"RHV {c.get('url')}: {e}")
    for c in cfg.get("rhel", []):
        from rhdisco.rhel import discover_rhel
        if c.get("hosts_file"):
            with open(os.path.expanduser(c.pop("hosts_file"))) as f:
                c["hosts"] = f.read()
        try:
            out["rhel"].append(discover_rhel(c, log))
        except Exception as e:
            log.error(f"RHEL: {e}")
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1, default=str)
    s = summarize(out)
    print(json.dumps(s["totals"], indent=2))
    print(f"Wrote {a.out}")
    if a.xlsx:
        from rhdisco.export import to_xlsx
        with open(a.xlsx, "wb") as f:
            f.write(to_xlsx(out, s))
        print(f"Wrote {a.xlsx}")


if __name__ == "__main__":
    main()
