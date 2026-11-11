import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from configuration import merge_env, parse_env
from migrate_legacy import migrate
import provision_secrets
from remote import Deployment, extract_archive, legacy_guard, switch_link, write_json


def archive_at(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = content.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.type = kind
                info.linkname = content
                tar.addfile(info)


class ConfigurationTests(unittest.TestCase):
    def test_secret_values_are_literal(self):
        secret = 'quote" slash\\ dollar$(touch /tmp/no)\nsecond line # hash'
        self.assertEqual(merge_env("HOST=127.0.0.1\nEMPTY=\n", json.dumps({"PASSWORD": secret})),
                         {"HOST": "127.0.0.1", "EMPTY": "", "PASSWORD": secret})

    def test_bad_configuration_is_rejected(self):
        for public, private in [
            ("PORT=1\nPORT=2", "{}"), ("HOST=x", '{"HOST":"y"}'),
            ("", '{"A":"1","A":"2"}'), ("", '{"PASSWORD":""}'),
            ("", '{"VITE_SECRET":"private"}'), ("", '{"COUNT":3}'),
            ("export HOST=x", "{}"), ("", '{"KEY":"a\\u0000b"}'),
            ("DATABASE_PASSWORD=accidental-literal", "{}"),
        ]:
            with self.subTest(public=public, private=private), self.assertRaises(ValueError):
                merge_env(public, private)

    def test_parse_error_does_not_echo_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, DEPLOY_APP="fastresearch", GITHUB_REPOSITORY="FastR-D/FastResearch",
                               GITHUB_SHA="a" * 40, GITHUB_REF_NAME="v1.0.0", GITHUB_RUN_ID="123",
                               BUNDLE_DIR=directory + "/bundle", RUNTIME_ENV="", RUNTIME_SECRETS="SUPERPRIVATE malformed")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/prepare.py")],
                                    env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SUPERPRIVATE", result.stdout + result.stderr)

    def test_prepare_checks_provenance_and_preserves_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            archive = artifact / "release.tar.gz"
            archive.write_bytes(b"artifact only: not extracted during prepare")
            manifest = {"repository": "FastR-D/FastResearch", "sha": "a" * 40,
                        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
            (artifact / "manifest.json").write_text(json.dumps(manifest))
            password = 'multiline\nquote" and \\ and $'
            environment = dict(os.environ, DEPLOY_APP="fastresearch", GITHUB_REPOSITORY=manifest["repository"],
                GITHUB_SHA=manifest["sha"], GITHUB_REF_NAME="v1.0.0", GITHUB_RUN_ID="123",
                ARTIFACT_DIR=str(artifact), BUNDLE_DIR=str(root / "bundle"),
                RUNTIME_ENV="HOST=127.0.0.1\nPORT=8787\nFASTRESEARCH_PUBLIC_URL=https://fastrd.io\nFASTRESEARCH_DATA_DIR=/home/dev/FastResearch/data",
                RUNTIME_SECRETS=json.dumps({"FASTRESEARCH_JWT_SECRET": password}))
            result = subprocess.run([sys.executable, str(ROOT / "scripts/prepare.py")],
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            request = root / "bundle/request.json"
            self.assertEqual(request.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(request.read_text())["env"]["FASTRESEARCH_JWT_SECRET"], password)
            self.assertNotIn(password, result.stdout + result.stderr)
            environment["BUNDLE_DIR"] = str(root / "invalid")
            manifest["sha"] = "b" * 40
            (artifact / "manifest.json").write_text(json.dumps(manifest))
            rejected = subprocess.run([sys.executable, str(ROOT / "scripts/prepare.py")],
                                      env=environment, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((root / "invalid/request.json").exists())


class ProvisionTests(unittest.TestCase):
    def test_existing_private_key_is_preserved_without_explicit_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "caller.yml"
            workflow.write_text('runtime-secrets: ${{ toJSON(secrets.TEST_PASSWORD) }}')
            metadata = {name: "value" for name in provision_secrets.SSH_NAMES if name != "FASTRD_SSH_PRIVATE_KEY"}
            with patch.object(sys, "argv", ["provision_secrets.py", "--apply", "--workflow", str(workflow)]), \
                    patch.object(provision_secrets, "existing", side_effect=[{"FASTRD_SSH_PRIVATE_KEY"}, set()]), \
                    patch.object(provision_secrets, "ssh_values", return_value=metadata) as source, \
                    patch.object(provision_secrets, "project_values", return_value={"TEST_PASSWORD": "secret"}), \
                    patch.object(provision_secrets, "set_secret") as write, redirect_stdout(io.StringIO()):
                provision_secrets.main()
                source.assert_called_once_with("fastrd", None)
                self.assertNotIn("FASTRD_SSH_PRIVATE_KEY", [call.args[0] for call in write.call_args_list])

    def test_transport_errors_retry_without_exposing_input(self):
        with patch.object(subprocess, "run", side_effect=[
            subprocess.CalledProcessError(1, ["gh"], stderr="unexpected EOF"),
            subprocess.CompletedProcess(["gh"], 0, stdout="ok"),
        ]) as run, patch.object(provision_secrets.time, "sleep"):
            self.assertEqual(provision_secrets.capture(["gh"], input="private"), "ok")
            self.assertEqual(run.call_count, 2)

    def test_permission_errors_do_not_retry(self):
        with patch.object(subprocess, "run", side_effect=
                          subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                provision_secrets.capture(["gh"])
            self.assertEqual(run.call_count, 1)

    def test_secret_is_only_passed_through_stdin(self):
        secret = 'private value\\"\nsecond line'
        with patch.object(provision_secrets, "capture", return_value="") as capture:
            with redirect_stdout(io.StringIO()) as output:
                provision_secrets.set_secret("NAME", secret, ["--repo", "FastR-D/FastResearch"])
            self.assertNotIn(secret, output.getvalue())
            args, kwargs = capture.call_args
            self.assertNotIn(secret, args[0])
            self.assertEqual(kwargs["input"], secret)

    def test_plan_does_not_read_or_write_remote_values(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "caller.yml"
            workflow.write_text('runtime-secrets: ${{ toJSON(secrets.TEST_PASSWORD) }}')
            with patch.object(sys, "argv", ["provision_secrets.py", "--workflow", str(workflow)]):
                with patch.object(provision_secrets, "capture") as capture:
                    with redirect_stdout(io.StringIO()):
                        provision_secrets.main()
                    capture.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def test_untrusted_archives_cannot_touch_release_or_parent(self):
        for name, content, kind in [
            ("../outside", "x", "file"), ("/tmp/outside", "x", "file"),
            ("server/.env.production", "secret", "file"),
            (".fastdeploy/runtime.json", "secret", "file"),
            ("node_modules/link", "../../outside", tarfile.SYMTYPE),
            ("node_modules/link", "server/index.mjs", tarfile.LNKTYPE),
            ("server/device", "", tarfile.CHRTYPE), ("data/accounts.sqlite", "x", "file"),
        ]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive_at(root / "archive.tar.gz", [("server/ok", "okay", "file"), (name, content, kind)])
                target = root / "release"
                target.mkdir()
                with self.assertRaises(ValueError):
                    extract_archive(root / "archive.tar.gz", target)
                self.assertEqual(list(target.iterdir()), [])


class Controller:
    def __init__(self):
        self.restarts = 0
        self.fail = False
        self.poller = False

    def restart(self, unit):
        self.restarts += 1
        if self.fail:
            self.fail = False
            raise RuntimeError("simulated restart failure")

    def legacy_active(self):
        return self.poller


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bundle = self.home / "bundle"
        self.bundle.mkdir()
        (self.bundle / "launch.py").write_bytes((ROOT / "scripts/launch.py").read_bytes())
        archive_at(self.bundle / "release.tar.gz", [("server/index.mjs", "// built artifact", "file")])
        profile = {
            "repository": "FastR-D/FastResearch", "legacy_app": "research", "unit": "fastresearch.service",
            "command": ["/usr/bin/node", "server/index.mjs"], "health_url": "http://127.0.0.1:8787/api/health",
            "required_files": ["server/index.mjs"], "required_env": ["PASSWORD"], "fixed_env": {},
            "shared_paths": [], "runtime_check": [sys.executable, "-c", "pass"],
        }
        self.request = {"app": "fastresearch", "profile": profile, "repository": profile["repository"],
                        "tag": "v1.0.0", "sha": "a" * 40, "run_id": 100,
                        "archive_sha256": hashlib.sha256((self.bundle / "release.tar.gz").read_bytes()).hexdigest(),
                        "env": {"PASSWORD": 'complex\n"secret\\'}}
        self.controller = Controller()
        self.check = lambda url: None
        self.dep = self.deployment()
        self.dep.root.mkdir(parents=True)
        self.original = self.home / "original"
        self.original.mkdir()
        switch_link(self.dep.current, self.original)

    def deployment(self, request=None, checker=None):
        return Deployment(self.home, request or self.request, self.controller, checker or self.check)

    def test_publish_and_idempotent_retry(self):
        self.dep.deploy(self.bundle)
        active = json.loads(self.dep.active.read_text())
        release = self.dep.current.resolve()
        self.assertEqual(active["sha"], "a" * 40)
        settings = release / ".fastdeploy/runtime.json"
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(settings.read_text())["env"], self.request["env"])
        self.dep.deploy(self.bundle)
        self.assertEqual(self.controller.restarts, 1)
        self.assertFalse(self.dep.transaction.exists())

    def test_health_failure_restores_original_link_and_dropin(self):
        calls = []
        def checker(url):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("unhealthy release")
        dep = self.deployment(checker=checker)
        dep.dropin.parent.mkdir(parents=True)
        dep.dropin.write_bytes(b"original dropin")
        with self.assertRaises(RuntimeError):
            dep.deploy(self.bundle)
        self.assertEqual(dep.current.resolve(), self.original)
        self.assertEqual(dep.dropin.read_bytes(), b"original dropin")
        self.assertEqual(self.controller.restarts, 2)
        self.assertEqual(len(calls), 2)
        self.assertFalse(dep.active.exists())

    def test_restart_failure_also_rolls_back(self):
        self.controller.fail = True
        with self.assertRaises(RuntimeError):
            self.dep.deploy(self.bundle)
        self.assertEqual(self.dep.current.resolve(), self.original)
        self.assertFalse(self.dep.dropin.exists())

    def test_recovery_record_survives_failed_restoration(self):
        def checker(url):
            raise RuntimeError("both release and recovery are unhealthy")
        dep = self.deployment(checker=checker)
        with self.assertRaises(RuntimeError):
            dep.deploy(self.bundle)
        self.assertTrue(dep.transaction.exists())
        self.dep.recover()
        self.assertFalse(dep.transaction.exists())
        self.assertEqual(dep.current.resolve(), self.original)

    def test_older_workflow_and_changed_same_run_are_rejected(self):
        self.dep.deploy(self.bundle)
        request = copy.deepcopy(self.request)
        request["run_id"] = 99
        with self.assertRaises(RuntimeError):
            self.deployment(request).deploy(self.bundle)
        request["run_id"] = 100
        request["env"]["PASSWORD"] = "changed"
        with self.assertRaises(RuntimeError):
            self.deployment(request).deploy(self.bundle)

    def test_manual_rollback_uses_expected_release_and_keeps_data(self):
        database = self.home / "business.sqlite"
        database.write_bytes(b"live business writes")
        self.dep.deploy(self.bundle)
        name = json.loads(self.dep.active.read_text())["release"]
        rollback_request = copy.deepcopy(self.request)
        rollback_request["run_id"] = 200
        rollback = self.deployment(rollback_request)
        with self.assertRaises(RuntimeError):
            rollback.rollback("unexpected")
        rollback.rollback(name)
        self.assertEqual(rollback.current.resolve(), self.original)
        self.assertFalse(rollback.dropin.exists())
        self.assertEqual(database.read_bytes(), b"live business writes")
        self.assertEqual(rollback.watermark.read_text(), "200")
        with self.assertRaises(RuntimeError):
            self.dep.deploy(self.bundle)

    def test_legacy_guard_migration_is_required_and_idempotent(self):
        self.controller.poller = True
        root = self.home / "ci-deploy"
        root.mkdir()
        script = root / "tag-deploy.sh"
        script.write_text('deploy_one() (\n  if ! flock -n 9; then\n    return 0\n  fi\n  tag="$(latest_tag "$repo")"\n)\n'
                          'baseline_one() {\n  tag="$(latest_tag "$repo")"\n}\n')
        script.chmod(0o750)
        with self.assertRaises(RuntimeError):
            self.dep.preflight()
        migrate(self.home, "research")
        migrate(self.home, "research")
        self.assertEqual(script.read_text().count(legacy_guard()), 1)
        self.assertEqual(script.stat().st_mode & 0o777, 0o750)
        self.dep.preflight()
        self.assertEqual(self.controller.restarts, 0)

    def test_launcher_preserves_secret_and_drops_stale_systemd_env(self):
        self.dep.deploy(self.bundle)
        release = self.dep.current.resolve()
        settings = release / ".fastdeploy/runtime.json"
        write_json(settings, {"env": self.request["env"], "command": [sys.executable, "-c",
            'import json,os; print(json.dumps({"password":os.environ["PASSWORD"],"stale":os.getenv("STALE")}))']})
        result = subprocess.run([sys.executable, str(release / ".fastdeploy/launch.py")],
                                env=dict(os.environ, STALE="old"), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"password": self.request["env"]["PASSWORD"], "stale": None})


if __name__ == "__main__":
    unittest.main()
