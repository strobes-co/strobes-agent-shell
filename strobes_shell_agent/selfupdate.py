"""Binary self-update for the shell bridge.

The bridge ships as a single PyInstaller executable. This module lets a running
bridge replace that executable in place with a newer release build and relaunch,
so a fleet of bridges tracks the latest agent WITHOUT anyone re-running the
installer on every host.

Two triggers, both server-driven ("when required to the bridge"):
  * ``identify_ack`` may carry ``required_agent_version`` + ``agent_url`` — the
    platform tells each bridge on connect what it should be running;
  * an explicit ``{"type": "update"}`` control message.
The bridge compares versions and, only if they differ, downloads + verifies +
swaps + relaunches. Nothing happens autonomously against GitHub unless
``STROBES_AGENT_URL`` is configured.

Design constraints (mirrors pack.py): NEVER raise into the daemon, ALWAYS verify
the sha256 when one is published, and only act when running as the frozen binary
(a source/pip install updates itself with pip, not by clobbering a file).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__
from . import pack  # reuse triple() + _sha256 helpers

log = logging.getLogger(__name__)

AGENT_URL_ENV = "STROBES_AGENT_URL"          # base URL to fetch agent binaries from
AUTO_UPDATE_DISABLE_ENV = "STROBES_NO_SELF_UPDATE"  # set truthy to refuse self-update


def current_version() -> str:
    return __version__


def _truthy(v: Optional[str]) -> bool:
    return bool(v) and v.lower() not in ("0", "false", "no", "off", "")


def asset_name() -> Optional[str]:
    """Release asset filename for this platform, matching release.yml's names."""
    return {
        "linux-x86_64": "strobes-shell-agent-linux-amd64",
        "linux-aarch64": "strobes-shell-agent-linux-arm64",
        "macos-aarch64": "strobes-shell-agent-macos-arm64",
        "windows-x86_64": "strobes-shell-agent-windows-amd64.exe",
    }.get(pack.triple())


def _fetch_expected_sha(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(url + ".sha256", timeout=30) as r:  # noqa: S310
            return r.read().decode().split()[0].strip()
    except Exception:  # noqa: BLE001
        return None


def can_self_update() -> bool:
    """Only a frozen PyInstaller binary can swap itself; a source install cannot."""
    if _truthy(os.environ.get(AUTO_UPDATE_DISABLE_ENV)):
        return False
    return bool(getattr(sys, "frozen", False))


def _download_binary(url: str, timeout: int = 600) -> Optional[Path]:
    """Download the new binary to a temp file, verifying its sha256 when published.
    Returns the temp path (caller owns it) or None. The temp file is created in the
    SAME directory as the current executable so the later os.replace is atomic
    (same filesystem), not a cross-device copy."""
    exe = Path(sys.executable).resolve()
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".strobes-update-", dir=str(exe.parent))
        os.close(fd)
        tmp = Path(tmp_name)
        log.info("downloading agent binary: %s", url)
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 (operator-configured URL)
        expected = _fetch_expected_sha(url)
        if expected:
            got = pack._sha256(tmp)
            if got != expected:
                log.error("agent sha256 mismatch (want %s got %s) — refusing", expected, got)
                tmp.unlink(missing_ok=True)
                return None
            log.info("agent sha256 verified")
        else:
            log.warning("no .sha256 alongside binary; skipping integrity check")
        return tmp
    except Exception as e:  # noqa: BLE001
        log.error("agent binary download failed: %s", e)
        return None


def _relaunch_posix(new_binary: Path, exe: Path) -> None:
    """Swap the running executable and re-exec. On Unix a running program keeps its
    open inode, so os.replace over the executable is safe; the next exec picks up
    the new file."""
    os.chmod(new_binary, 0o755)
    os.replace(new_binary, exe)  # atomic, same dir
    log.info("agent binary replaced; relaunching %s", exe)
    os.execv(str(exe), [str(exe), *sys.argv[1:]])  # never returns


def _relaunch_windows(new_binary: Path, exe: Path) -> None:
    """Windows cannot overwrite a running .exe, so hand the swap to a detached
    batch script: it waits for THIS process to exit, moves the new binary over the
    old one, then relaunches with the original args. We then exit cleanly."""
    args = " ".join(f'"{a}"' for a in sys.argv[1:])
    bat = exe.parent / "strobes-update.bat"
    bat.write_text(
        "@echo off\r\n"
        "ping 127.0.0.1 -n 3 >nul\r\n"           # ~2s grace for this process to die
        f':retry\r\n'
        f'move /y "{new_binary}" "{exe}" >nul 2>&1\r\n'
        f'if errorlevel 1 (ping 127.0.0.1 -n 2 >nul & goto retry)\r\n'
        f'start "" "{exe}" {args}\r\n'
        f'del "%~f0"\r\n'
    )
    log.info("agent update staged; a helper will swap and relaunch on exit")
    subprocess.Popen(["cmd", "/c", str(bat)],  # noqa: S603,S607
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    os._exit(0)  # let the helper take over; the service manager restarts us anyway


def apply_update(agent_url: Optional[str] = None, expected_version: Optional[str] = None,
                 timeout: int = 600) -> bool:
    """Download the new agent binary and swap+relaunch. Returns False if it did not
    (or could not) update; on a successful update it does NOT return — the process
    re-execs (POSIX) or exits for the helper (Windows). Never raises."""
    try:
        if not can_self_update():
            log.info("self-update skipped: not a frozen binary (source install)")
            return False
        base = agent_url or os.environ.get(AGENT_URL_ENV)
        if not base:
            log.info("self-update skipped: no agent URL configured")
            return False
        name = asset_name()
        if not name:
            log.warning("self-update skipped: no asset for triple %s", pack.triple())
            return False
        # base may be a full asset URL or a release base to which we append the name
        url = base if base.endswith(name) else base.rstrip("/") + "/" + name
        tmp = _download_binary(url, timeout)
        if not tmp:
            return False
        exe = Path(sys.executable).resolve()
        if os.name == "nt":
            _relaunch_windows(tmp, exe)
        else:
            _relaunch_posix(tmp, exe)
        return True  # unreachable on success
    except Exception as e:  # noqa: BLE001 — an update must never crash the daemon
        log.error("self-update failed: %s", e)
        return False


def maybe_update(required_version: Optional[str], agent_url: Optional[str] = None) -> bool:
    """Update only when the server asks for a version different from ours. Returns
    True if an update was attempted (process is re-execing / exiting)."""
    if not required_version or required_version == current_version():
        return False
    log.info("agent update required: have %s, want %s", current_version(), required_version)
    return apply_update(agent_url=agent_url, expected_version=required_version)
