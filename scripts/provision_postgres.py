#!/usr/bin/env python3
"""Create dedicated app databases on the existing production PostgreSQL listener."""

import argparse
import json
import os
from pathlib import Path
import secrets
from urllib.parse import quote

from postgres_tools import identifier, literal, psql_env, run_psql


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="+", choices=("fastresearch", "fastread", "fasttask", "fastnews", "fastwrite", "fastppt", "fastfigure"))
    parser.add_argument("--psql", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    home = Path.home()
    control = home / "ci-releases/cas/current/.fastdeploy/runtime.json"
    admin_env = psql_env(json.loads(control.read_text())["env"]["FASTCAS_DATABASE_URL"])
    if admin_env["PGHOST"] != "127.0.0.1" or admin_env["PGPORT"] != "5432":
        raise RuntimeError("FastCAS is not configured on the expected local PostgreSQL instance")
    directory = home / ".local/state/fastdeploy/postgres"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for app in args.apps:
        path = directory / app
        if path.exists():
            value = path.read_text().strip()
            runtime = psql_env(value)
            actual = run_psql(args.psql, runtime, "SELECT current_database()||'|'||current_user;").strip()
            if actual != f"{app}|{app}":
                raise RuntimeError(f"{app} has an unexpected PostgreSQL database/role")
            print(f"{app} database verified")
            continue
        used = run_psql(args.psql, admin_env,
                        f"SELECT EXISTS(SELECT FROM pg_roles WHERE rolname={literal(app)}) OR "
                        f"EXISTS(SELECT FROM pg_database WHERE datname={literal(app)});").strip()
        if used != "f":
            raise RuntimeError(f"{app} database or role already exists without the server credential file")
        password = secrets.token_urlsafe(32)
        dsn = f"postgresql://{app}:{quote(password,safe='')}@127.0.0.1:5432/{app}?sslmode=disable"
        # Stage the credential first so any partial provisioning can be repaired
        # with the same password and without granting a second login.
        staged = path.with_suffix(".pending")
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(dsn + "\n")
            output.flush()
            os.fsync(output.fileno())
        owner = identifier(app)
        sql = f"""
SET password_encryption='scram-sha-256';
CREATE ROLE {owner} LOGIN PASSWORD {literal(password)} CONNECTION LIMIT 25;
CREATE DATABASE {owner} OWNER {owner};
REVOKE CONNECT ON DATABASE {owner} FROM PUBLIC;
GRANT CONNECT ON DATABASE {owner} TO {owner};
"""
        run_psql(args.psql, admin_env, sql)
        if run_psql(args.psql, psql_env(dsn), "SELECT current_database()||'|'||current_user;").strip() != f"{app}|{app}":
            raise RuntimeError(f"{app} PostgreSQL login could not be verified")
        staged.rename(path)
        print(f"{app} database provisioned and verified")


if __name__ == "__main__":
    main()
