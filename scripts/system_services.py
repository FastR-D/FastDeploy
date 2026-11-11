#!/usr/bin/python3
"""Root-owned bridge for retiring/restoring only legacy FastRead/FastWrite units."""
import json
import os
from pathlib import Path
import subprocess
import sys

UNITS = {
    "fastread": ["fastread-web.service", "fastread-worker.service"],
    "fastwrite": ["fastwrite.service"],
}
ROOT = Path("/var/lib/fastdeploy/system-services")


def systemctl(*args, check=True):
    return subprocess.run(["/usr/bin/systemctl", *args], check=check, capture_output=True,
                          text=True, timeout=90, env={"PATH": "/usr/bin:/bin", "LANG": "C"})


def main():
    if os.geteuid() != 0 or len(sys.argv) != 3:
        raise SystemExit("requires root and exactly two arguments")
    operation, app = sys.argv[1:]
    if app not in UNITS or operation not in {"check", "quiesce", "restore"}:
        raise SystemExit("unsupported fixed service operation")
    if operation == "check":
        return
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = ROOT / (app + ".json")
    if operation == "quiesce":
        if not state.exists():
            snapshot = {}
            for unit in UNITS[app]:
                active = systemctl("is-active", unit, check=False).stdout.strip()
                if active not in {"active", "inactive", "failed"}:
                    raise SystemExit("legacy service is transitioning; retry later")
                snapshot[unit] = {
                    "active": active == "active",
                    "enabled": systemctl("is-enabled", unit, check=False).stdout.strip() == "enabled",
                }
            temporary = state.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot))
            os.replace(temporary, state)
        # Repeating also completes an interrupted first handover.
        for unit in UNITS[app]:
            systemctl("disable", "--now", unit)
    else:
        if not state.exists():
            return
        snapshot = json.loads(state.read_text())
        if set(snapshot) != set(UNITS[app]):
            raise SystemExit("invalid protected recovery record")
        for unit in UNITS[app]:
            if snapshot[unit]["enabled"]:
                systemctl("enable", unit)
            if snapshot[unit]["active"]:
                systemctl("start", unit)
        state.unlink()


if __name__ == "__main__":
    try:
        main()
    except subprocess.SubprocessError:
        raise SystemExit("fixed system service operation failed")
