#!/usr/bin/env python3
"""Upload a private bundle with pinned SSH host keys; no credentials in argv."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import uuid


def main():
    os.umask(0o077)
    bundle = Path(os.environ["BUNDLE_DIR"])
    host = os.environ["SSH_HOST"]
    user = os.environ["SSH_USER"]
    port = os.environ["SSH_PORT"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
        raise ValueError("invalid SSH hostname")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("invalid SSH username")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid SSH port")
    if not os.environ["SSH_PRIVATE_KEY"].strip() or not os.environ["SSH_KNOWN_HOSTS"].strip():
        raise ValueError("SSH key and pinned known_hosts are required")
    with tempfile.TemporaryDirectory(prefix="fastdeploy-ssh-") as directory:
        root = Path(directory)
        (root / "key").write_text(os.environ["SSH_PRIVATE_KEY"] + "\n")
        (root / "known_hosts").write_text(os.environ["SSH_KNOWN_HOSTS"] + "\n")
        config = root / "config"
        config.write_text(
            f"Host fastrd\n  HostName {host}\n  User {user}\n  Port {port}\n"
            f"  IdentityFile {root}/key\n  UserKnownHostsFile {root}/known_hosts\n"
            "  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n"
            "  IdentitiesOnly yes\n  BatchMode yes\n  ConnectTimeout 15\n"
            "  ServerAliveInterval 15\n  ServerAliveCountMax 4\n"
            "  ForwardAgent no\n  LogLevel ERROR\n"
        )
        ssh = ["ssh", "-F", str(config), "fastrd"]
        incoming = ".local/state/fastdeploy/incoming/" + uuid.uuid4().hex
        remote = shlex.quote(incoming)
        archive = root / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(bundle.iterdir()):
                if not path.is_file() or path.is_symlink():
                    raise ValueError("invalid upload bundle")
                tar.add(path, arcname=path.name)
        try:
            with archive.open("rb") as stream:
                subprocess.run(ssh + [f"umask 077; mkdir -p {remote} && tar -xzf - -C {remote}"],
                               stdin=stream, check=True, timeout=300)
            subprocess.run(ssh + [f"python3 {remote}/remote.py {remote}"], check=True, timeout=600)
        finally:
            subprocess.run(ssh + [f"rm -rf -- {remote}"], timeout=30, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("SSH transfer or remote deployment failed")
