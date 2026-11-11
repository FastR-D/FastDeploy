"""Publish only public FastRead frontend files into the existing Nginx root.

The API's private release and runtime configuration never become web-readable.
Directory exchanges are atomic on the Linux deployment host.
"""
import base64
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import uuid


def exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "atomic public directory exchange failed")


def link(path, target):
    temporary = path.with_name(path.name + ".fastdeploy-next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    if path.is_dir() and not path.is_symlink():
        exchange(temporary, path)
        shutil.rmtree(temporary)
    else:
        os.replace(temporary, path)


def snapshot_public(config):
    root = Path(config["root"]).resolve(strict=True)
    result = {"root": str(root), "entries": {}}
    for name in ["Read", "index.html", "vendor"]:
        path = root / name
        if path.is_symlink():
            state = {"kind": "link", "target": os.readlink(path)}
        elif path.is_file():
            state = {"kind": "file", "data": base64.b64encode(path.read_bytes()).decode()}
        elif path.is_dir():
            backup = root / ".fastdeploy/backups" / uuid.uuid4().hex
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(path, backup)
            state = {"kind": "directory", "backup": str(backup)}
        else:
            state = {"kind": "missing"}
        result["entries"][name] = state
    return result


def restore_public(config, snapshot):
    root = Path(snapshot["root"])
    for name, state in snapshot["entries"].items():
        path = root / name
        if state["kind"] == "directory":
            backup = Path(state["backup"])
            temporary = root / (name + ".fastdeploy-restore-" + uuid.uuid4().hex)
            shutil.copytree(backup, temporary)
            if path.exists() or path.is_symlink():
                exchange(path, temporary)
                if temporary.is_symlink():
                    temporary.unlink()
                else:
                    shutil.rmtree(temporary)
            else:
                os.replace(temporary, path)
        elif state["kind"] == "link":
            link(path, state["target"])
        elif state["kind"] == "file":
            temporary = path.with_name(path.name + ".fastdeploy-next")
            temporary.unlink(missing_ok=True)
            temporary.write_bytes(base64.b64decode(state["data"]))
            temporary.chmod(0o644)
            os.replace(temporary, path)
        else:
            path.unlink(missing_ok=True)


def publish(config, release, name):
    # Load the exact pre-activation snapshot, already recorded in the transaction.
    import json
    snapshot = json.loads((release / ".fastdeploy/previous.json").read_text())["public"]
    root = Path(snapshot["root"])
    target = root / ".fastdeploy/releases" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / ".fastdeploy").chmod(0o755)
    target.parent.chmod(0o755)
    shutil.copytree(release / config["source"], target)
    for directory, _, files in os.walk(target):
        Path(directory).chmod(0o755)
        for filename in files:
            (Path(directory) / filename).chmod(0o644)
    # Keep old content-addressed assets available for already-open browser tabs.
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    for source in (target / "assets").rglob("*"):
        destination = assets / source.relative_to(target / "assets")
        if source.is_dir():
            if not destination.exists():
                destination.mkdir(parents=True)
                destination.chmod(0o755)
        else:
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                    raise ValueError("public asset name collision")
            else:
                shutil.copyfile(source, destination)
                destination.chmod(0o644)
    shutil.rmtree(target / "assets")
    (target / "assets").symlink_to(assets)
    for name, source in [("Read", target), ("index.html", target / "index.html"), ("vendor", target / "vendor")]:
        if not source.exists():
            raise ValueError("required public frontend entry is missing")
        link(root / name, source)
