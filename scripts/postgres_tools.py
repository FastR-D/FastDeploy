"""psql access without putting database credentials in arguments or output."""

import os
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, unquote, urlsplit


def psql_env(dsn):
    parsed = urlsplit(dsn)
    if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or not parsed.username:
        raise ValueError("invalid PostgreSQL URL")
    env = dict(os.environ, PGHOST=parsed.hostname, PGPORT=str(parsed.port or 5432),
               PGUSER=unquote(parsed.username), PGPASSWORD=unquote(parsed.password or ""),
               PGDATABASE=unquote(parsed.path.lstrip("/")), PGOPTIONS="-c TimeZone=UTC")
    for key, setting in parse_qs(parsed.query).items():
        if key in ("host", "sslmode", "sslrootcert", "sslcert", "sslkey"):
            env["PG" + key.upper()] = setting[0]
    return env


def run_psql(binary, env, statements):
    result = subprocess.run([str(binary), "-wXqAt", "-v", "ON_ERROR_STOP=1"],
                            input=statements, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", env=env, timeout=300)
    if result.returncode:
        # Errors from PostgreSQL can echo credentials or imported row contents.
        error_lines = [line for line in result.stderr.splitlines() if "ERROR:" in line]
        if error_lines and os.environ.get("FASTDEPLOY_DB_DEBUG") == "1" and Path(binary).resolve().is_file():
            raise RuntimeError(error_lines[0])
        raise RuntimeError("PostgreSQL operation failed") from None
    return result.stdout


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"
