"""Shared collection logic for TA-sanpath (multipath + iSCSI)."""
import json
import os
import re
import subprocess
import sys


def run(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def _exists(*paths):
    for p in paths:
        if os.path.exists(p):
            return True
    return False


_MAPDRAW_RE = re.compile(r"^[\s|`+\-_\\]+")
_MODERN_MAP_RE = re.compile(r"^(\S+)\s+\(.*\)\s+dm-\d+(?:\s|$)")
_DMDEV_RE = re.compile(r"^dm-\d+$")
_WWID_RE = re.compile(r"^\(?[0-9a-fA-F]{16,}\)?$")
_HCTID_RE = re.compile(r"^\d+:\d+:\d+:\d+$")
_DISK_RE = re.compile(r"^(?:sd[a-z]+|hd[a-z]+|xvd[a-z]+|nvme\d+n\d+(?:p\d+)?|dm-\d+)$")
_MAJMIN_RE = re.compile(r"^\d+:\d+$")
_STATE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def parse_map_line(line):
    m = _MODERN_MAP_RE.match(line)
    if m:
        return m.group(1)
    toks = line.split()
    if not toks or not any(_DMDEV_RE.match(t) for t in toks):
        return None
    for t in toks:
        if _WWID_RE.match(t):
            return t.strip("()")
    return None


def parse_path_line(line):
    s = _MAPDRAW_RE.sub("", line).strip()
    if not s:
        return None
    toks = s.split()
    i = 0
    if _HCTID_RE.match(toks[0]):
        i = 1
    if i >= len(toks) or not _DISK_RE.match(toks[i]):
        return None
    i += 1
    if i < len(toks) and _MAJMIN_RE.match(toks[i]):
        i += 1
    if i >= len(toks) or not _STATE_RE.match(toks[i]):
        return None
    state = toks[i]
    ready = toks[i + 1] if i + 1 < len(toks) and _STATE_RE.match(toks[i + 1]) else None
    active = state == "active"
    failed = state == "failed" or ready in ("failed", "faulty")
    if not active and not failed:
        return {"active": False, "failed": False}
    return {"active": active, "failed": failed}


def parse_multipath_output(out):
    maps = {}
    mp = None
    for line in out.splitlines():
        if not line.strip():
            continue
        name = parse_map_line(line)
        if name:
            mp = name
            maps[mp] = {"active_paths": 0, "failed_paths": 0, "total_paths": 0}
            continue
        if mp is None:
            continue
        p = parse_path_line(line)
        if p:
            maps[mp]["total_paths"] += 1
            if p["active"]:
                maps[mp]["active_paths"] += 1
            if p["failed"]:
                maps[mp]["failed_paths"] += 1
    return maps


def _daemon_issue():
    return _exists("/sys/class/scsi_device") and _exists("/run/multipathd.socket")


def collect_multipath():
    if not _exists("/sbin/multipath", "/usr/sbin/multipath", "/usr/bin/multipath"):
        return {}
    rc, out = run(["multipath", "-l"])
    maps = parse_multipath_output(out)
    if maps and rc == 0:
        return maps
    # empty/failed listing: fall back to /sys count if daemon lists nothing
    n = 0
    try:
        n = len([d for d in os.listdir("/sys/class/multipath") if not d.startswith(".")])
    except OSError:
        n = 0
    if n > 0:
        return {"_count_only": {"active_paths": 0, "failed_paths": 0}}
    # tool present + scsi devices + live multipathd socket, yet we got nothing:
    # that is a permissions/daemon problem, not an honest "no multipath"
    if _daemon_issue():
        return None
    return {}


def collect_iscsi():
    if not _exists("/usr/sbin/iscsiadm", "/usr/bin/iscsiadm"):
        return 0
    _rc, out = run(["iscsiadm", "-m", "session"])
    return sum(1 for l in out.splitlines() if re.match(r"^tcp:\s", l))


def discover():
    mps = collect_multipath()
    daemon_issue = mps is None
    if daemon_issue:
        mps = {}
    iscsi = collect_iscsi()
    status = "ok"
    for m in mps.values():
        if m.get("failed_paths", 0) > 0 or (m.get("total_paths") and m["active_paths"] == 0):
            status = "alarm"
    present = bool(mps) or iscsi > 0
    if daemon_issue:
        status = "permission_or_daemon_issue"
    elif not present:
        status = "absent"
    data = {
        "status": status,
        "multipaths": mps,
        "iscsi_sessions": iscsi,
    }
    if daemon_issue:
        data["multipath_daemon_issue"] = 1
    return data


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
