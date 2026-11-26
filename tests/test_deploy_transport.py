import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest
import urllib.error
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("deploy_transport", Path(__file__).resolve().parents[1] / "scripts" / "transport.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class TransportDiagnosticsTests(unittest.TestCase):
    def test_success_reports_phase_without_subprocess_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            transport.run_stage("ssh_probe", [sys.executable, "-c", "print('private-host private-key')"], timeout=10)
        self.assertIn("stage=ssh_probe event=start timeout_seconds=10", output.getvalue())
        self.assertIn("stage=ssh_probe event=success elapsed_seconds=", output.getvalue())
        self.assertNotIn("private-host", output.getvalue())
        self.assertNotIn("private-key", output.getvalue())

    def test_failed_authentication_is_classified_without_secrets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(transport.DeploymentFailure) as caught:
            transport.run_stage("upload", [sys.executable, "-c", "import sys; sys.exit('Permission denied (publickey) for private-user private-key')"], timeout=10)
        self.assertIn("reason=ssh_authentication_rejected exit_code=1", output.getvalue())
        self.assertIn("upload: ssh_authentication_rejected", str(caught.exception))
        self.assertNotIn("private-user", output.getvalue())
        self.assertNotIn("private-key", output.getvalue())

    def test_timeout_kills_subprocess_and_identifies_stage(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(transport.DeploymentFailure) as caught:
            transport.run_stage("upload", [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.05)
        self.assertIn("stage=upload event=timeout", output.getvalue())
        self.assertIn("timeout_seconds=0.05", output.getvalue())
        self.assertIn("upload: timeout", str(caught.exception))

    def test_start_failure_does_not_expose_command_or_path(self):
        output = io.StringIO()
        with patch.object(transport.subprocess, "Popen", side_effect=OSError("secret command path")):
            with contextlib.redirect_stdout(output), self.assertRaises(transport.DeploymentFailure) as caught:
                transport.run_stage("activate", ["secret-command"], timeout=10)
        self.assertNotIn("secret", output.getvalue() + str(caught.exception))
        self.assertIn("activate: process_start_failed", str(caught.exception))

    def test_cleanup_failure_preserves_original_failure(self):
        output = io.StringIO()
        calls = []

        def fail(stage, *_args, **_kwargs):
            calls.append(stage)
            raise transport.DeploymentFailure(stage + ": failure")

        with patch.object(transport, "run_stage", side_effect=fail), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(transport.DeploymentFailure, "ssh_probe: failure"):
                transport.transfer([], "safe-path", Path("unused"), upload_timeout=600, activate_timeout=600)
        self.assertEqual(calls, ["ssh_probe", "cleanup"])
        self.assertIn("remote_cleanup_incomplete", output.getvalue())

    def test_transfer_stages_have_distinct_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive"
            archive.write_bytes(b"test")
            with patch.object(transport, "run_stage") as run:
                transport.transfer(["ssh"], "safe-path", archive, upload_timeout=600, activate_timeout=900)
        self.assertEqual([call.args[0] for call in run.call_args_list], ["ssh_probe", "upload", "activate", "cleanup"])
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [30, 600, 900, 30])
        self.assertIn("python3 -u", run.call_args_list[2].args[1][-1])

    def test_unknown_output_and_invalid_timeout_stay_private(self):
        self.assertEqual(transport.failure_kind("private host and application secret"), "command_failed")
        with patch.dict(os.environ, {"FASTDEPLOY_UPLOAD_TIMEOUT": "secret-value"}):
            with self.assertRaisesRegex(transport.DeploymentFailure, "configuration: invalid_timeout"):
                transport.timeout_setting("FASTDEPLOY_UPLOAD_TIMEOUT")

    def test_https_url_uses_encrypted_stdin_and_separate_download_stage(self):
        url = "https://example.blob.core.windows.net/artifact?sig=private-signature"
        calls = []

        def capture(stage, command, **kwargs):
            calls.append((stage, command))
            self.assertNotIn("private-signature", str(command))
            if stage == "download_artifact":
                self.assertEqual(json.load(kwargs["stdin"]), {"url": url})

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive"
            archive.write_bytes(b"test")
            with patch.object(transport, "run_stage", side_effect=capture):
                transport.transfer(["ssh"], "safe-path", archive, upload_timeout=600, activate_timeout=600, artifact_url=url)
        self.assertEqual([stage for stage, _ in calls], ["ssh_probe", "upload", "download_artifact", "activate", "cleanup"])

    def test_github_token_is_not_forwarded_to_artifact_storage(self):
        url = "https://example.blob.core.windows.net/artifact?sig=private-signature"
        listing = io.BytesIO(json.dumps({"artifacts": [{"id": 123, "name": "release-" + "a" * 40, "expired": False}]}).encode())
        redirect = urllib.error.HTTPError("https://api.github.com/artifact", 302, "redirect", {"Location": url}, None)
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private-token", "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "123", "GITHUB_SHA": "a" * 40}):
            with patch.object(transport.urllib.request, "build_opener") as builder, contextlib.redirect_stdout(output):
                builder.return_value.open.side_effect = [listing, redirect]
                self.assertEqual(transport.artifact_url(), url)
                for call in builder.return_value.open.call_args_list:
                    self.assertTrue(call.args[0].full_url.startswith("https://api.github.com/"))
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, None, None, None, None))
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("private-signature", output.getvalue())

    def test_custom_artifact_name_is_respected(self):
        url = "https://example.blob.core.windows.net/artifact?sig=private-signature"
        listing = io.BytesIO(json.dumps({"artifacts": [{"id": 123, "name": "custom-release", "expired": False}]}).encode())
        redirect = urllib.error.HTTPError("https://api.github.com/artifact", 302, "redirect", {"Location": url}, None)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private-token", "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "123", "GITHUB_SHA": "a" * 40, "ARTIFACT_NAME": "custom-release"}):
            with patch.object(transport.urllib.request, "build_opener") as builder, contextlib.redirect_stdout(io.StringIO()):
                builder.return_value.open.side_effect = [listing, redirect]
                self.assertEqual(transport.artifact_url(), url)

    def test_rollback_does_not_request_artifact_with_token_present(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "request.json").write_text('{"operation":"rollback"}')
            settings = {"BUNDLE_DIR": str(bundle), "SSH_HOST": "private-host", "SSH_USER": "private_user", "SSH_PORT": "22", "SSH_PRIVATE_KEY": "private-key", "SSH_KNOWN_HOSTS": "private-host-key", "GITHUB_TOKEN": "private-token"}
            with patch.dict(os.environ, settings), patch.object(transport, "artifact_url") as lookup, patch.object(transport, "transfer") as transfer, contextlib.redirect_stdout(io.StringIO()):
                transport.main()
                lookup.assert_not_called()
                self.assertIsNone(transfer.call_args.kwargs["artifact_url"])

    def test_https_bootstrap_omits_large_archive_and_contains_downloader(self):
        url = "https://example.blob.core.windows.net/artifact?sig=private-signature"
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "release.tar.gz").write_bytes(b"large archive")
            (bundle / "request.json").write_text("{}")
            (bundle / "remote.py").write_text("# reviewed remote activation")

            def inspect(_ssh, _remote, archive, **kwargs):
                self.assertEqual(kwargs["artifact_url"], url)
                with tarfile.open(archive, "r:gz") as contents:
                    self.assertEqual(set(contents.getnames()), {"request.json", "remote.py", "download-artifact.py"})
                    self.assertNotIn(b"private-signature", contents.extractfile("download-artifact.py").read())

            settings = {"BUNDLE_DIR": str(bundle), "SSH_HOST": "private-host", "SSH_USER": "private_user", "SSH_PORT": "22", "SSH_PRIVATE_KEY": "private-key", "SSH_KNOWN_HOSTS": "private-host-key", "GITHUB_TOKEN": "private-token"}
            with patch.dict(os.environ, settings), patch.object(transport, "artifact_url", return_value=url):
                with patch.object(transport, "transfer", side_effect=inspect) as transfer, contextlib.redirect_stdout(output):
                    transport.main()
                    transfer.assert_called_once()
        for private in ("private-host", "private-key", "private-token", "private-signature"):
            self.assertNotIn(private, output.getvalue())


if __name__ == "__main__":
    unittest.main()
