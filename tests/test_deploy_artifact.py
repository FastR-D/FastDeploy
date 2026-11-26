import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


spec = importlib.util.spec_from_file_location("deploy_artifact", Path(__file__).resolve().parents[1] / "scripts" / "deploy-artifact.py")
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


class ArtifactDownloadTests(unittest.TestCase):
    def archive(self, payload=b"verified release", extra=None, manifest=None):
        expected = {"repository": "example/repo", "sha": "a" * 40, "archive_sha256": hashlib.sha256(b"verified release").hexdigest()}
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("release.tar.gz", payload)
            archive.writestr("manifest.json", json.dumps(expected if manifest is None else manifest))
            if extra:
                archive.writestr(extra, "unexpected")
        data.seek(0)
        return data, expected

    def test_downloaded_artifact_matches_the_original_ci_archive(self):
        archive, expected = self.archive()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            artifact.unpack(archive, destination, expected)
            self.assertEqual((destination / "release.tar.gz").read_bytes(), b"verified release")
            self.assertFalse((destination / ".release.tar.gz.part").exists())

    def test_corrupt_release_is_rejected_and_partial_output_removed(self):
        archive, expected = self.archive(payload=b"tampered")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with self.assertRaisesRegex(artifact.ArtifactFailure, "checksum_mismatch"):
                artifact.unpack(archive, destination, expected)
            self.assertEqual(list(destination.iterdir()), [])

    def test_unexpected_paths_are_rejected_before_writing(self):
        archive, expected = self.archive(extra="../escape")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with self.assertRaisesRegex(artifact.ArtifactFailure, "invalid_archive"):
                artifact.unpack(archive, destination, expected)
            self.assertEqual(list(destination.iterdir()), [])

    def test_artifact_from_another_commit_is_rejected(self):
        archive, expected = self.archive(manifest={"repository": "example/repo", "sha": "b" * 40, "archive_sha256": "f" * 64})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(artifact.ArtifactFailure, "checksum_mismatch"):
                artifact.unpack(archive, Path(directory), expected)

    def test_oversized_manifest_is_rejected(self):
        archive, expected = self.archive(manifest={"private_data": "x" * 65536})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(artifact.ArtifactFailure, "invalid_archive"):
                artifact.unpack(archive, Path(directory), expected)


if __name__ == "__main__":
    unittest.main()
