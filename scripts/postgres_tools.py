"""psql access without putting database credentials in arguments or output."""

import os
import re
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
    result = subprocess.run([str(binary), "-wXqAt", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"],
                            input=statements, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", env=env, timeout=300)
    if result.returncode:
        # Errors from PostgreSQL can echo credentials or imported row contents.
        state = re.search(r"ERROR:\s+([A-Z0-9]{5}):", result.stderr)
        location = re.search(r"CONTEXT:\s+COPY (import_[a-z0-9_]+), line ([0-9]+)", result.stderr)
        details = f" ({state[1]})" if state else ""
        column = re.search(r'ERROR:\s+42703: column "([a-z0-9_]+)" does not exist', result.stderr)
        if column:
            details += f"; missing column {column[1]}"
        if location:
            details += f" in {location[1]}, row {location[2]}"
        raise RuntimeError("PostgreSQL operation failed" + details) from None
    return result.stdout


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"
