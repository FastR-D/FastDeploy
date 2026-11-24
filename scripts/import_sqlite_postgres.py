#!/usr/bin/env python3
"""Copy SQLite rows into an initialized PostgreSQL schema and verify equality."""

import argparse
from collections import defaultdict
import io
import json
import os
from pathlib import Path
import sqlite3

from postgres_tools import identifier, literal, psql_env, run_psql


def connect_source(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def sqlite_snapshot(source, destination):
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with connect_source(source) as original, sqlite3.connect(destination) as target:
            original.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("SQLite snapshot integrity check failed")
    except BaseException:
        destination.unlink()
        raise


def catalog(psql, env):
    return json.loads(run_psql(psql, env, """
SELECT COALESCE(json_agg(c ORDER BY table_name,ordinal_position),'[]') FROM (
 SELECT table_name,column_name,data_type,is_identity,ordinal_position
 FROM information_schema.columns
 WHERE table_schema=current_schema() AND (table_schema,table_name) IN (
    SELECT table_schema,table_name FROM information_schema.tables WHERE table_type='BASE TABLE')
 ) c;
"""))


def csv_field(value, datatype):
    if value is None:
        return r"\N"
    if isinstance(value, bytes):
        if datatype != "bytea":
            raise ValueError("binary SQLite field requires a bytea target")
        value = r"\x" + value.hex()
    else:
        value = str(value)
    if "\x00" in value:
        raise ValueError("SQLite text contains a zero byte unsupported by PostgreSQL")
    return '"' + value.replace('"', '""') + '"'


def import_rows(psql, env, sources, archive, apply):
    columns = defaultdict(dict)
    for column in catalog(psql, env):
        columns[column["table_name"]][column["column_name"]] = column
    if not columns:
        raise RuntimeError("initialize the PostgreSQL schema before importing")
    checks, staged, inserts, verifications, sequences, counts = [], [], [], [], [], {}
    items = sorted(columns)
    lock = "LOCK TABLE " + ",".join(map(identifier, items)) + " IN ACCESS EXCLUSIVE MODE;"
    seeds = {"schema_migrations", "schema_versions", "knowledge_migrations"}
    skipped = set()
    for source in sources:
        with connect_source(source) as db:
            db.execute("BEGIN")
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for name in tables:
                if name not in columns:
                    if archive:
                        skipped.add(name)
                        continue
                    raise ValueError(f"no PostgreSQL target for SQLite table {name}")
                if name in counts:
                    raise ValueError(f"duplicate source table {name}")
                table = identifier(name)
                temp = identifier("import_" + name)
                target = columns[name]
                fields = [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
                if set(fields) - target.keys():
                    raise ValueError(f"unmapped source columns in {name}")
                projection = ",".join(map(identifier, fields))
                copy = io.StringIO()
                copy.write(f"CREATE TEMP TABLE {temp} (LIKE {table} INCLUDING DEFAULTS);\n")
                copy.write(f"\\copy {temp} ({projection}) FROM STDIN WITH (FORMAT csv, NULL '\\N')\n")
                count = 0
                for row in db.execute(f"SELECT {projection} FROM {table}"):
                    copy.write(",".join(csv_field(v, target[f]["data_type"]) for f, v in zip(fields, row)) + "\n")
                    count += 1
                copy.write("\\.\n")
                counts[name] = count
                staged.append(copy.getvalue())
                if name in seeds:
                    checks.append(f"DELETE FROM {table};")
                else:
                    checks.append(f"DO $$ BEGIN IF EXISTS(SELECT FROM {table}) THEN RAISE EXCEPTION 'target is not empty'; END IF; END $$;")
                inserts.append(f"INSERT INTO {table} ({projection}) SELECT {projection} FROM {temp};")
                # Extra target columns retain the defaults of the newer schema.
                verifications.append(f"""DO $$ BEGIN
 IF (SELECT count(*) FROM {table}) <> {count} OR EXISTS (
  SELECT {projection} FROM {table} EXCEPT ALL SELECT {projection} FROM {temp}
 ) THEN RAISE EXCEPTION 'import verification mismatch'; END IF;
 END $$;""")
                for field in fields:
                    if target[field]["is_identity"] == "YES" and count:
                        sequences.append(f"SELECT setval(pg_get_serial_sequence({literal(table)},{literal(field)}),(SELECT max({identifier(field)}) FROM {table}));")
    if not counts:
        raise ValueError("no matching source tables")
    fk = json.loads(run_psql(psql, env, """
SELECT COALESCE(json_agg(f),'[]') FROM (
 SELECT cls.relname AS "table",conname AS "constraint"
 FROM pg_constraint JOIN pg_class cls ON cls.oid=conrelid JOIN pg_namespace n ON n.oid=cls.relnamespace
 WHERE n.nspname=current_schema() AND contype='f' AND NOT condeferrable
) f;
"""))
    alters = [
        f'ALTER TABLE {identifier(row["table"])} ALTER CONSTRAINT {identifier(row["constraint"])}'
        for row in fk
    ]
    statements = ["BEGIN;", lock, *checks, *(a + " DEFERRABLE;" for a in alters),
                  "SET CONSTRAINTS ALL DEFERRED;", *staged, *inserts, "SET CONSTRAINTS ALL IMMEDIATE;",
                  *verifications, *(a + " NOT DEFERRABLE;" for a in alters)]
    statements += [*sequences, "COMMIT;"] if apply else ["ROLLBACK;"]
    run_psql(psql, env, "\n".join(statements))
    return {"applied": apply, "tables": len(counts), "rows": sum(counts.values()),
            "verified": True, "archived_source_tables": sorted(skipped)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, nargs="+")
    parser.add_argument("--psql", default="psql")
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--archive-extra-source-tables", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sources = args.sources
    if args.snapshot_dir:
        args.snapshot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in sources:
            sqlite_snapshot(path, args.snapshot_dir / path.name)
        sources = [args.snapshot_dir / p.name for p in sources]
    report = import_rows(args.psql, psql_env(os.environ["FASTDEPLOY_DATABASE_URL"]),
                         sources, args.archive_extra_source_tables, args.apply)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
