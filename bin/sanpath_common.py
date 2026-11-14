"""Shared collection logic for TA-sanpath (multipath + iSCSI)."""
import json
import os
import re
import subprocess
import sys


def run(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _exists(*paths):
    for p in paths:
        if os.path.exists(p):
            return True
    return False


def collect_multipath():
    if not _exists("/sbin/multipath", "/usr/sbin/multipath", "/usr/bin/multipath"):
        return {}
    out = run(["multipath", "-l"])
    if not out.strip():
        # fall back to /sys count if daemon lists nothing
        n = 0
        try:
            n = len([d for d in os.listdir("/sys/class/multipath") if not d.startswith(".")])
        except OSError:
            n = 0
        return {} if n == 0 else {"_count_only": {"active_paths": 0, "failed_paths": 0}}
    out_p = {}
    mp = None
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+\(.*\)\s+dm-\d+\s", line)
        if m:
            mp = m.group(1)
            out_p[mp] = {"active_paths": 0, "failed_paths": 0, "total_paths": 0}
            continue
        m2 = re.match(r"^\s*[|`][-+]+\s+(\d+:\d+:\d+:\d+)\s+(\w+)\s+(\w+)\s+(\w+)", line)
        if m2 and mp:
            path_state, ready = m2.group(2), m2.group(3)
            out_p[mp]["total_paths"] += 1
            if path_state == "active" and ready == "ready":
                out_p[mp]["active_paths"] += 1
            elif path_state == "failed" or ready in ("faulty", "faulty"):
                out_p[mp]["failed_paths"] += 1
    return out_p


def collect_iscsi():
    if not _exists("/usr/sbin/iscsiadm", "/usr/bin/iscsiadm"):
        return 0
    out = run(["iscsiadm", "-m", "session"])
    return sum(1 for l in out.splitlines() if re.match(r"^tcp:\s", l))


def discover():
    mps = collect_multipath()
    iscsi = collect_iscsi()
    status = "ok"
    for m in mps.values():
        if m.get("failed_paths", 0) > 0 or (m.get("total_paths") and m["active_paths"] == 0):
            status = "alarm"
    present = bool(mps) or iscsi > 0
    return {
        "status": "absent" if not present else status,
        "multipaths": mps,
        "iscsi_sessions": iscsi,
    }


def metrics_payload(data):
    payload = {"multipath_count": len(data.get("multipaths", {})), "iscsi_session_count": data.get("iscsi_sessions", 0)}
    for name, m in data.get("multipaths", {}).items():
        for key in ("active_paths", "failed_paths", "total_paths"):
            if key in m:
                payload["%s_%s" % (name, key)] = m[key]
    return payload


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def fail(message):
    sys.stderr.write("TA-sanpath collection error: %s\n" % message)
    return 1
