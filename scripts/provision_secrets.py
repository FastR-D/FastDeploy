#!/usr/bin/env python3
"""Provision org SSH and FastResearch repository Secrets without printing values."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
ORG = "FastR-D"
REPOSITORY = ORG + "/FastResearch"
SSH_REPOSITORIES = "FastResearch,FastNews,FastWrite,FastRead,FastTask,FastCAS,FastInsight,FastLabs"
SSH_NAMES = {"FASTRD_SSH_HOST", "FASTRD_SSH_PORT", "FASTRD_SSH_USER",
             "FASTRD_SSH_PRIVATE_KEY", "FASTRD_SSH_KNOWN_HOSTS"}


def capture(args, input=None):
    # Operations here are reads or idempotent Secret PUTs. Retry transport errors
    # only; permission failures must still stop before reading source credentials.
    for attempt in range(3):
        try:
            return subprocess.run(args, input=input, capture_output=True, text=True, check=True, timeout=60).stdout
        except subprocess.CalledProcessError as error:
            transport_error = any(fragment in (error.stderr or "").lower() for fragment in
                                  ["eof", "tls", "connection reset", "timed out", "temporarily unavailable"])
            if not transport_error or attempt == 2:
                raise
            time.sleep(attempt + 1)


def project_names(workflow):
    return sorted(set(re.findall(r"toJSON\(secrets\.([A-Z_][A-Z0-9_]*)\)", workflow.read_text())))


def existing(scope):
    return {item["name"] for item in json.loads(capture(["gh", "secret", "list", *scope, "--json", "name"]))}


def ssh_values(alias):
    rows = capture(["ssh", "-G", alias]).splitlines()
    settings = {}
    for row in rows:
        key, _, value = row.partition(" ")
        settings[key] = value
    hostname, port = settings["hostname"], settings["port"]
    lookup = hostname if port == "22" else f"[{hostname}]:{port}"
    pinned = []
    for path in shlex.split(settings["userknownhostsfile"]):
        path = Path(os.path.expanduser(path.replace("%d", str(Path.home()))))
        if path.is_file():
            result = subprocess.run(["ssh-keygen", "-F", lookup, "-f", str(path)],
                                    capture_output=True, text=True, check=False, timeout=10)
            pinned.extend(line for line in result.stdout.splitlines() if line and not line.startswith("#"))
    if not pinned:
        raise RuntimeError("no trusted known_hosts entry found")
    # This is the existing dedicated CI key, not the user's personal SSH key.
    key = capture(["ssh", "-o", "BatchMode=yes", alias,
                   "cat /home/dev/ci-deploy/github-actions-deploy"])
    public = capture(["ssh", "-o", "BatchMode=yes", alias,
                      "ssh-keygen -y -f /home/dev/ci-deploy/github-actions-deploy"]).strip()
    authorized = capture(["ssh", "-o", "BatchMode=yes", alias, "cat /home/dev/.ssh/authorized_keys"])
    # Compare the algorithm and base64 key, ignoring comments and key options.
    if not public or public not in authorized:
        raise RuntimeError("dedicated CI key is not authorized")
    return {"FASTRD_SSH_HOST": hostname, "FASTRD_SSH_PORT": port,
            "FASTRD_SSH_USER": settings["user"], "FASTRD_SSH_PRIVATE_KEY": key,
            "FASTRD_SSH_KNOWN_HOSTS": "\n".join(pinned) + "\n"}


def project_values(alias, names):
    # Node's env-file parser matches the current npm start entry point. stdout is
    # captured in memory and never emitted; no secrets are interpolated into code.
    code = "const names=" + json.dumps(names) + ";const result=Object.fromEntries(names.map(k=>[k,process.env[k]??'']));"
    # Empty legacy JWT env means the app uses its persisted JWT key. Preserve it.
    code += ("if(!result.FASTRESEARCH_JWT_SECRET){const fs=require('node:fs');"
             "result.FASTRESEARCH_JWT_SECRET=JSON.parse(fs.readFileSync('/home/dev/FastResearch/data/access.json','utf8')).jwtSecret??'';}"
             "process.stdout.write(JSON.stringify(result));")
    command = shlex.join(["/usr/bin/node", "--env-file=/home/dev/FastResearch/.env.production", "-e", code])
    return json.loads(capture(["ssh", "-o", "BatchMode=yes", alias, command]))


def set_secret(name, value, scope):
    if not isinstance(value, str) or not value:
        raise ValueError("empty secret value")
    # Secret is on stdin, never argv, a shell expression or a local file.
    capture(["gh", "secret", "set", name, *scope], input=value)
    print(f"Configured {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path,
                        default=ROOT.parent / "FastResearch/.github/workflows/tag-ci.yml")
    parser.add_argument("--ssh-alias", default="fastrd")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly permit replacing existing same-name secrets")
    args = parser.parse_args()
    names = project_names(args.workflow)
    if not names:
        raise RuntimeError("no project secret mappings found")
    print("Organization SSH Secrets: " + ", ".join(sorted(SSH_NAMES)))
    print("Repository Secrets: " + ", ".join(names))
    if not args.apply:
        print("Plan only. No remote values read or GitHub settings changed. Use --apply to provision.")
        return
    org_scope = ["--org", ORG]
    repo_scope = ["--repo", REPOSITORY]
    conflicts = (existing(org_scope) & SSH_NAMES) | (existing(repo_scope) & set(names))
    if conflicts and not args.overwrite:
        raise RuntimeError("existing Secrets would be replaced; inspect them before using --overwrite")
    organization = ssh_values(args.ssh_alias)
    project = project_values(args.ssh_alias, names)
    if set(project) != set(names) or any(not isinstance(value, str) or not value for value in project.values()):
        raise RuntimeError("source has missing or empty project secrets; no values were changed")
    for name, value in sorted(organization.items()):
        set_secret(name, value, org_scope + ["--repos", SSH_REPOSITORIES])
    for name, value in sorted(project.items()):
        set_secret(name, value, repo_scope)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Exceptions may contain ssh/gh response bodies; don't echo private values.
        raise SystemExit("Secret provisioning failed; check permissions, existing names and source completeness")
