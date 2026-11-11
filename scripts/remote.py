#!/usr/bin/env python3
"""Transactional user-systemd release activation. Standard library only."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request


class DeployError(RuntimeError):
    """An operator-facing message containing no application output or secret data."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode())


def switch_link(path, target):
    temporary = path.with_name(path.name + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def extract_archive(archive, destination, allowed_roots=None):
    """Reject links, devices, traversal, configuration and writable application data."""
    allowed_roots = set(allowed_roots or ["server", "dist", "node_modules", "package.json", "package-lock.json"])
    seen = set()
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 200000:
            raise ValueError("too many archive entries")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("unsafe archive path")
            if path in seen or not (member.isfile() or member.isdir()):
                raise ValueError("duplicate archive entry or unsupported file type")
            seen.add(path)
            if any(part in {".git", ".ssh", ".fastdeploy"} or part == ".env"
                   or part.startswith(".env.") for part in path.parts):
                raise ValueError("archive contains private configuration")
            if path.parts[0] not in allowed_roots:
                raise ValueError("file outside application artifact allowlist")
            total += member.size
            if total > 2 * 1024**3:
                raise ValueError("archive exceeds expanded size limit")
        # All members were inspected before writing anything; links are prohibited.
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)


class Systemd:
    def __init__(self):
        self.env = dict(os.environ)
        self.env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        self.env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.env['XDG_RUNTIME_DIR']}/bus"

    def run(self, *args):
        subprocess.run(["systemctl", "--user", *args], env=self.env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)

    def state(self, operation, unit):
        result = subprocess.run(["systemctl", "--user", operation, unit], env=self.env,
            capture_output=True, text=True, timeout=10)
        if operation == "is-active" and result.stdout.strip() in {"activating", "deactivating", "reloading"}:
            raise DeployError("service is transitioning; retry deployment later")
        return result.returncode == 0

    def legacy_active(self):
        for unit in ["fast-tag-deploy.timer", "fast-tag-deploy.service"]:
            result = subprocess.run(["systemctl", "--user", "is-active", unit], env=self.env,
                                    capture_output=True, text=True, timeout=10)
            if result.stdout.strip() in {"active", "activating", "reloading", "deactivating"}:
                return True
        return False

    def restart(self, unit):
        self.run("daemon-reload")
        self.run("restart", unit)
        self.run("is-active", unit)


def health(url, attempts=30):
    # Loopback only in the trusted profile. Ignore inherited HTTP proxy variables.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(attempts):
        try:
            with opener.open(url, timeout=3) as response:
                if response.status == 200:
                    if url.endswith("#http200"):
                        return
                    body = json.load(response)
                    if isinstance(body, dict) and (body.get("ok") is True or body.get("ready") is True or body.get("status") in {"ok", "healthy", "ready"}):
                        return
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(1)
    raise DeployError("application readiness check failed")


class Deployment:
    def __init__(self, home, request, controller=None, check_health=health):
        self.home, self.request = Path(home), request
        self.profile = request["profile"]
        self.app = request["app"]
        if not re.fullmatch(r"[a-z][a-z0-9-]*", self.app):
            raise ValueError("invalid app")
        legacy = self.profile["legacy_app"]
        if not re.fullmatch(r"[a-z]+", legacy):
            raise ValueError("invalid legacy app")
        self.root = self.home / "ci-releases" / legacy
        self.releases = self.root / "fastdeploy-releases"
        self.current = self.root / "current"
        self.active = self.root / "fastdeploy-active.json"
        self.transaction = self.root / "fastdeploy-transaction.json"
        self.dropin = self.home / ".config/systemd/user" / (self.profile["unit"] + ".d") / "zzzz-fastdeploy.conf"
        self.controller = controller or Systemd()
        self.check_health = check_health
        self.watermark = self.root / "fastdeploy-run-watermark"
        self.services = self.profile.get("services")

    def ready(self, verify_services=True):
        if self.profile.get("health_url"):
            self.check_health(self.profile["health_url"])
            if verify_services and self.services:
                for service in self.services:
                    self.controller.run("is-active", service["unit"])
        else:
            # A bot has no HTTP endpoint. Require a stable process, without sending messages.
            for _ in range(10):
                for service in self.services:
                    self.controller.run("is-active", service["unit"])
                time.sleep(1)

    def unit_path(self, service):
        return self.home / ".config/systemd/user" / service["unit"]

    def service_dropin(self, service):
        return self.unit_path(service).with_name(service["unit"] + ".d") / "zzzz-fastdeploy.conf"

    def quiesce_transient(self):
        if not self.profile.get("transient_legacy"):
            return
        for service in self.services:
            path = Path(f"/run/user/{os.getuid()}/systemd/transient") / service["unit"]
            if path.exists():
                self.controller.run("stop", service["unit"])
                # A transient definition has higher priority than a persistent unit.
                # Wait for systemd to collect it before installing its replacement.
                for _ in range(30):
                    if not path.exists():
                        break
                    time.sleep(0.1)
                if path.exists():
                    raise DeployError("legacy transient unit was not collected")

    def install_services(self):
        for service in self.services:
            path = self.unit_path(service)
            if not path.exists():
                atomic_write(path, ("[Unit]\nDescription=FastDeploy " + self.app + "\nAfter=network.target\n"
                    "[Service]\nType=simple\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=60\nUMask=0077\n"
                    "NoNewPrivileges=true\n[Install]\nWantedBy=default.target\n").encode())
            dropin = ("[Service]\nEnvironment=\nEnvironmentFile=\nPassEnvironment=\n"
                f"WorkingDirectory={self.current}\nExecStart=\n"
                f"ExecStart=/usr/bin/python3 {self.current}/.fastdeploy/launch.py {service['unit']}\n")
            atomic_write(self.service_dropin(service), dropin.encode())

    def restart_services(self):
        for service in self.services:
            self.controller.restart(service["unit"])
            self.controller.run("enable", service["unit"])

    def system_helper(self, operation):
        app = self.profile.get("system_legacy")
        if app:
            subprocess.run(["sudo", "-n", "/usr/local/sbin/fastdeploy-system-services", operation, app],
                check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


    def lock(self):
        path = self.home / "ci-releases/locks" / (self.profile["legacy_app"] + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = path.open("a")
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return stream
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise DeployError("another deployment holds the project lock")
                time.sleep(1)

    def preflight(self):
        # A legacy poller must either be stopped or have the post-lock ownership guard.
        script = self.home / "ci-deploy/tag-deploy.sh"
        marker = self.home / "ci-deploy/fastdeploy-managed" / self.profile["legacy_app"]
        guarded = script.exists() and legacy_guard() in script.read_text() and marker.exists()
        if self.controller.legacy_active() and not guarded:
            raise DeployError("legacy poller still owns this project; run migrate_legacy.py first")
        if self.current.exists() and not self.current.is_symlink():
            raise DeployError("current must be a symlink")
        self.system_helper("check")
        for path in self.profile.get("create_paths", []):
            Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.profile["shared_paths"]:
            if not Path(path).is_dir():
                raise DeployError("required shared data directory is missing")

    def snapshot(self):
        if self.services:
            states = []
            for service in self.services:
                path, dropin = self.unit_path(service), self.service_dropin(service)
                transient = Path(f"/run/user/{os.getuid()}/systemd/transient") / service["unit"]
                original = path if path.exists() else transient
                states.append({"unit": service["unit"],
                    "base": base64.b64encode(original.read_bytes()).decode() if original.exists() else None,
                    "dropin": base64.b64encode(dropin.read_bytes()).decode() if dropin.exists() else None,
                    "active": self.controller.state("is-active", service["unit"]),
                    "enabled": self.controller.state("is-enabled", service["unit"])})
            snapshot = {"current": os.readlink(self.current) if self.current.is_symlink() else None,
                "services": states, "active": json.loads(self.active.read_text()) if self.active.exists() else None}
            if self.profile.get("public_static"):
                from publication import snapshot_public
                snapshot["public"] = snapshot_public(self.profile["public_static"])
            return snapshot
        return {
            "current": os.readlink(self.current) if self.current.is_symlink() else None,
            "dropin": base64.b64encode(self.dropin.read_bytes()).decode() if self.dropin.exists() else None,
            "active": json.loads(self.active.read_text()) if self.active.exists() else None,
        }

    def restore(self, snapshot):
        if "services" in snapshot:
            return self.restore_services(snapshot)
        if snapshot["current"] is None:
            self.current.unlink(missing_ok=True)
        else:
            switch_link(self.current, snapshot["current"])
        if snapshot["dropin"] is None:
            self.dropin.unlink(missing_ok=True)
        else:
            atomic_write(self.dropin, base64.b64decode(snapshot["dropin"]))
        self.controller.restart(self.profile["unit"])
        self.check_health(self.profile["health_url"])
        if snapshot["active"] is None:
            self.active.unlink(missing_ok=True)
        else:
            write_json(self.active, snapshot["active"])

    def restore_services(self, snapshot):
        for service in self.services:
            if self.controller.state("is-active", service["unit"]):
                self.controller.run("stop", service["unit"])
        if snapshot["current"] is None:
            self.current.unlink(missing_ok=True)
        else:
            switch_link(self.current, snapshot["current"])
        for state in snapshot["services"]:
            service = {"unit": state["unit"]}
            if self.unit_path(service).exists():
                self.controller.run("disable", state["unit"])
            for path, value in [(self.unit_path(service), state["base"]), (self.service_dropin(service), state["dropin"])]:
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, base64.b64decode(value))
        self.controller.run("daemon-reload")
        for state in snapshot["services"]:
            if state["enabled"]:
                self.controller.run("enable", state["unit"])
            if state["active"]:
                self.controller.restart(state["unit"])
        if self.profile.get("system_legacy") and snapshot["active"] is None:
            self.system_helper("restore")
        elif self.profile.get("system_legacy"):
            self.system_helper("quiesce")
        if "public" in snapshot:
            from publication import restore_public
            restore_public(self.profile["public_static"], snapshot["public"])
        if snapshot["active"] is not None or any(state["active"] for state in snapshot["services"]) or self.profile.get("system_legacy"):
            self.ready(verify_services=not (self.profile.get("system_legacy") and snapshot["active"] is None))
        if snapshot["active"] is None:
            self.active.unlink(missing_ok=True)
        else:
            write_json(self.active, snapshot["active"])

    def recover(self):
        if self.transaction.exists():
            self.restore(json.loads(self.transaction.read_text()))
            self.transaction.unlink()
            print("Recovered interrupted transaction")

    def deploy(self, bundle):
        request = self.request
        if self.watermark.exists() and request["run_id"] < int(self.watermark.read_text()):
            raise DeployError("workflow predates the last deployment or rollback")
        if request["repository"] != self.profile["repository"]:
            raise ValueError("repository mismatch")
        archive = bundle / "release.tar.gz"
        if sha256_file(archive) != request["archive_sha256"]:
            raise ValueError("archive checksum mismatch")
        for key in self.profile["required_env"]:
            if not request["env"].get(key):
                raise ValueError("missing required runtime variable")
        for key, value in self.profile["fixed_env"].items():
            if request["env"].get(key) != value:
                raise ValueError("runtime variable conflicts with profile")
        previous = self.snapshot()
        identity = {key: request[key] for key in ["tag", "sha", "run_id", "archive_sha256"]}
        config_digest = hashlib.sha256(json.dumps(request["env"], sort_keys=True).encode()).hexdigest()
        identity["config_sha256"] = config_digest
        active = previous["active"]
        if active:
            if request["run_id"] < active["run_id"]:
                raise DeployError("older workflow run cannot replace a newer deployment")
            if all(active.get(key) == value for key, value in identity.items()):
                self.ready()
                print("Release already active and healthy")
                return
            if request["run_id"] == active["run_id"]:
                raise DeployError("same run ID has different release content; start a new run")
        name = f"{request['tag']}-{request['sha'][:12]}-{request['run_id']}"
        if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]{0,99}-[0-9a-f]{12}-[0-9]+", name):
            raise ValueError("invalid release identity")
        self.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.releases / name
        if destination.exists():
            # A failed attempt remains available for diagnosis; never overwrite it.
            raise DeployError("release directory exists; use a new workflow run")
        destination.mkdir(mode=0o700)
        extract_archive(archive, destination, self.profile.get("artifact_roots"))
        for path in self.profile["required_files"]:
            if not (destination / path).is_file():
                raise DeployError("artifact missing a required application file")
        subprocess.run(self.profile["runtime_check"], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        settings = destination / ".fastdeploy"
        settings.mkdir(mode=0o700)
        shutil.copyfile(bundle / "launch.py", settings / "launch.py")
        runtime_env = dict(request["env"])
        # Expand only trusted profile templates, never user-provided values or Secrets.
        runtime_env.update({key: value.replace("{release}", str(destination)) for key, value in self.profile.get("release_env", {}).items()})
        runtime = {"env": runtime_env, "command": self.profile.get("command", [])}
        if self.services:
            runtime["services"] = {service["unit"]: {
                "command": [value.replace("{release}", str(destination)) for value in service["command"]],
                "cwd": service.get("cwd", "{release}").replace("{release}", str(destination)),
                "pre_start": [[value.replace("{release}", str(destination)) for value in command]
                              for command in service.get("pre_start", [])],
            } for service in self.services}
        write_json(settings / "runtime.json", runtime)
        write_json(settings / "previous.json", previous)
        identity["release"] = name
        write_json(settings / "release.json", identity)
        dropin = ("[Service]\nEnvironment=\nEnvironmentFile=\nPassEnvironment=\n"
                  f"WorkingDirectory={self.current}\nExecStart=\n"
                  f"ExecStart=/usr/bin/python3 {self.current}/.fastdeploy/launch.py\n")
        write_json(self.transaction, previous)
        try:
            if self.services:
                self.quiesce_transient()
                self.install_services()
            else:
                atomic_write(self.dropin, dropin.encode())
            switch_link(self.current, destination)
            if self.services:
                self.system_helper("quiesce")
                self.restart_services()
            else:
                self.controller.restart(self.profile["unit"])
            self.ready()
            if self.profile.get("public_static"):
                from publication import publish
                publish(self.profile["public_static"], destination, name)
            write_json(self.active, identity)
            atomic_write(self.watermark, str(request["run_id"]).encode())
            self.transaction.unlink()
        except BaseException:
            self.restore(previous)
            self.transaction.unlink()
            print("Activation failed; previous service and configuration restored")
            raise
        print(f"Deployed {self.app} {name}")

    def rollback(self, expected):
        if not self.active.exists():
            raise DeployError("no FastDeploy release is active")
        active = json.loads(self.active.read_text())
        if not expected or active["release"] != expected:
            raise DeployError("active release changed; refusing rollback")
        previous_file = self.releases / active["release"] / ".fastdeploy/previous.json"
        previous = json.loads(previous_file.read_text())
        current_snapshot = self.snapshot()
        write_json(self.transaction, current_snapshot)
        try:
            self.restore(previous)
            prior = int(self.watermark.read_text()) if self.watermark.exists() else 0
            atomic_write(self.watermark, str(max(prior, self.request["run_id"])).encode())
            self.transaction.unlink()
        except BaseException:
            self.restore(current_snapshot)
            self.transaction.unlink()
            raise
        print("Previous service and configuration restored; database unchanged")


def legacy_guard():
    return ('  # FastDeploy ownership guard (must remain after flock).\n'
            '  if [ -f "$CI_ROOT/fastdeploy-managed/$app" ]; then\n'
            '    log "managed by FastDeploy; skipping legacy deployment"\n'
            '    return 0\n'
            '  fi\n')


def main():
    os.umask(0o077)
    bundle = Path(sys.argv[1]).resolve()
    request = json.loads((bundle / "request.json").read_text())
    deployment = Deployment(Path.home(), request)
    def interrupted(*_):
        raise DeployError("deployment interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        with deployment.lock():
            deployment.preflight()
            deployment.recover()
            if request.get("operation") == "rollback":
                deployment.rollback(request["expected_release"])
            else:
                deployment.deploy(bundle)
    finally:
        shutil.rmtree(bundle)


if __name__ == "__main__":
    try:
        main()
    except DeployError as error:
        raise SystemExit(f"FastDeploy: {error}")
    except Exception:
        # Application/parser output may contain secret material. Never echo it to CI.
        raise SystemExit("FastDeploy failed; deployment preflight, activation or recovery did not complete")
