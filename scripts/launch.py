#!/usr/bin/env python3
"""Inject a release's exact environment; never parse it as shell source."""
import json
import os
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    settings = json.loads((root / ".fastdeploy/runtime.json").read_text())
    # Do not inherit stale EnvironmentFile/Environment entries from the old unit.
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        **settings["env"],
    }
    os.chdir(root)
    command = settings["command"]
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
