#!/usr/bin/env python3
"""One-time, explicit per-project handover; run ON THE SERVER as dev."""
import argparse
from pathlib import Path
import shutil
import time

from remote import Deployment, atomic_write, legacy_guard


def migrate(home, legacy_app):
    profile = {"legacy_app": legacy_app, "unit": "unused.service"}
    deployment = Deployment(home, {"app": "fastresearch", "profile": profile})
    with deployment.lock():
        root = Path(home) / "ci-deploy"
        script = root / "tag-deploy.sh"
        text = script.read_text()
        marker = root / "fastdeploy-managed" / legacy_app
        anchor = '  tag="$(latest_tag "$repo")"'
        # The baseline helper also calls latest_tag. Only patch the locked body.
        if legacy_guard() not in text:
            head, start, remaining = text.partition("deploy_one() (\n")
            body, end, tail = remaining.partition("\n)\n")
            if (not start or not end or body.splitlines().count(anchor) != 1
                    or '  if ! flock -n 9; then' not in body
                    or body.index('  if ! flock -n 9; then') > body.index(anchor)):
                raise RuntimeError("legacy script changed; review the ownership insertion manually")
            backup = script.with_name(f"tag-deploy.sh.before-fastdeploy-{time.time_ns()}")
            shutil.copy2(script, backup)
            mode = script.stat().st_mode & 0o777
            replacement = head + start + body.replace(anchor, legacy_guard() + anchor) + end + tail
            atomic_write(script, replacement.encode())
            script.chmod(mode)
        atomic_write(marker, b"FastDeploy owns this app. Remove only after disabling its Actions deployment.\n")
        print(f"Legacy polling disabled for {legacy_app}; service was not restarted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=["research"], required=True)
    args = parser.parse_args()
    migrate(Path.home(), args.app)
