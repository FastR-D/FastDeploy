#!/usr/bin/env python3
"""Validate CI inputs and write a private upload bundle."""
import json
import os
from pathlib import Path
import re
import shutil

from configuration import merge_env
from remote import sha256_file


def main():
    os.umask(0o077)
    source = Path(__file__).resolve().parent.parent
    profiles = json.loads((source / "config/apps.json").read_text())
    app = os.environ["DEPLOY_APP"]
    if app not in profiles:
        raise ValueError("unsupported app")
    profile = profiles[app]
    repository = os.environ["GITHUB_REPOSITORY"]
    operation = os.environ.get("DEPLOY_OPERATION", "deploy")
    if repository != profile["repository"]:
        raise ValueError("caller repository does not match app")
    bundle = Path(os.environ["BUNDLE_DIR"])
    bundle.mkdir(mode=0o700)
    for name in ["remote.py", "launch.py", "publication.py"]:
        shutil.copyfile(source / "scripts" / name, bundle / name)
    if operation == "rollback":
        expected = os.environ["EXPECTED_RELEASE"]
        if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]{0,99}-[0-9a-f]{12}-[0-9]+", expected):
            raise ValueError("invalid expected release")
        request = {"operation": "rollback", "app": app, "profile": profile,
                   "expected_release": expected, "run_id": int(os.environ["GITHUB_RUN_ID"])}
        (bundle / "request.json").write_text(json.dumps(request))
        return
    sha = os.environ["GITHUB_SHA"]
    tag = os.environ["GITHUB_REF_NAME"]
    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]{0,99}", tag):
        raise ValueError("deployment requires a v* version tag")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid commit SHA")
    environment = merge_env(os.environ["RUNTIME_ENV"], os.environ["RUNTIME_SECRETS"])
    for key in profile["required_env"]:
        if not environment.get(key):
            raise ValueError(f"missing required environment key: {key}")
    for key, value in profile["fixed_env"].items():
        if environment.get(key) != value:
            raise ValueError(f"{key} must match the server profile")
    artifact = Path(os.environ["ARTIFACT_DIR"])
    archive = artifact / "release.tar.gz"
    manifest = json.loads((artifact / "manifest.json").read_text())
    digest = sha256_file(archive)
    if manifest != {"repository": repository, "sha": sha, "archive_sha256": digest}:
        raise ValueError("artifact provenance or checksum mismatch")
    shutil.copyfile(archive, bundle / "release.tar.gz")
    request = {
        "app": app, "profile": profile, "repository": repository, "sha": sha,
        "tag": tag, "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "archive_sha256": digest, "env": environment,
    }
    (bundle / "request.json").write_text(json.dumps(request, ensure_ascii=False))
    print(f"Validated release: {app} {tag} {sha}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Parser exceptions may include fragments of secret input.
        raise SystemExit("Release validation failed; check input names, required values and artifact manifest")
