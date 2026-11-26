#!/usr/bin/env python3
"""Download the original CI artifact privately, then verify its release checksum."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit
import zipfile

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


class ArtifactFailure(RuntimeError):
    pass


def unpack(archive, destination, expected):
    with zipfile.ZipFile(archive) as package:
        files = package.infolist()
        if len(files) != 2 or {file.filename for file in files} != {"release.tar.gz", "manifest.json"}:
            raise ArtifactFailure("invalid_archive")
        if package.getinfo("manifest.json").file_size > 65536 or package.getinfo("release.tar.gz").file_size > MAX_ARCHIVE_BYTES:
            raise ArtifactFailure("invalid_archive")
        manifest = json.loads(package.read("manifest.json"))
        if any(manifest.get(key) != expected[key] for key in ("repository", "sha", "archive_sha256")):
            raise ArtifactFailure("checksum_mismatch")
        partial = destination / ".release.tar.gz.part"
        try:
            digest = hashlib.sha256()
            with package.open("release.tar.gz") as source, partial.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected["archive_sha256"]:
                raise ArtifactFailure("checksum_mismatch")
            os.replace(partial, destination / "release.tar.gz")
        finally:
            partial.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    destination = Path(sys.argv[1]).resolve()
    expected = json.loads((destination / "request.json").read_text())
    url = json.load(sys.stdin)["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not (parsed.hostname or "").endswith((".blob.core.windows.net", ".actions.githubusercontent.com")):
        raise ArtifactFailure("invalid_download_origin")
    with tempfile.TemporaryFile() as archive:
        total = 0
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ArtifactFailure("invalid_archive")
                    archive.write(chunk)
        except (TimeoutError, socket.timeout):
            raise ArtifactFailure("timeout") from None
        except OSError:
            raise ArtifactFailure("network_failed") from None
        archive.seek(0)
        unpack(archive, destination, expected)


if __name__ == "__main__":
    try:
        main()
    except ArtifactFailure as error:
        raise SystemExit(f"artifact_download: {error}")
    except Exception:
        raise SystemExit("artifact_download: invalid_archive")
