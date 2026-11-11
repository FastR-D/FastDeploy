#!/usr/bin/env python3
"""Inject a release's exact environment; never parse it as shell source."""
import json
import os
import sys
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    settings = json.loads((root / ".fastdeploy/runtime.json").read_text())
    # Do not inherit stale EnvironmentFile/Environment entries from the old unit.
    environment = {
        "PATH": "/home/dev/.npm-global/bin:/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        **settings["env"],
    }
    service = settings.get("services", {}).get(sys.argv[1] if len(sys.argv) > 1 else "", settings)
    os.chdir(service.get("cwd", root))
    for command in service.get("pre_start", []):
        result = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=180)
        if result.returncode:
            raise SystemExit("FastDeploy: application pre-start failed")
    command = service["command"]
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
