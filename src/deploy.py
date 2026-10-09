"""Publicera metadata från Git till Azure SQL (metadata.Source, metadata.IngestionObject).

    python src/deploy.py [--dry-run]

Anslutning: miljövariabeln METADATA_DB_CONNECTION_STRING (ODBC connection string).
--dry-run jämför inte mot databasen utan visar vad som skulle skapas.

Skriver aldrig till runtime.*, och raderar aldrig något.
"""
import argparse
import json
import os
import subprocess
import sys

from validate import DEFAULT_ROOT, load_and_validate

SOURCE_COLS = ["source_type", "connection_ref", "config_json"]
OBJECT_COLS = ["source_id", "source_schema", "source_table", "query_text",
               "load_type", "watermark_column", "keys_json", "enabled"]


def source_row(s):
    return {
        "source_type": s["type"],
        "connection_ref": s["connection"]["ref"],
        "config_json": json.dumps({"defaults": s.get("defaults", {})}, sort_keys=True),
    }


def object_row(t):
    return {
        "source_id": t["source"],
        "source_schema": t["schema"],
        "source_table": t["table"],
        "query_text": t["query_text"],
        "load_type": t["load"]["type"],
        "watermark_column": t["load"].get("watermark"),
        "keys_json": json.dumps(t["keys"]),
        "enabled": 1 if t["enabled"] else 0,
    }


def upsert(cur, table, key_col, key, cols, new, commit):
    """INSERT, UPDATE eller ingen skrivning. Returnerar 'created' | 'updated' | 'unchanged'."""
    cur.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {key_col} = ?", key)
    old = cur.fetchone()
    if old is None:
        names = [key_col, *cols, "git_commit"]
        cur.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            key, *[new[c] for c in cols], commit)
        return "created"
    if all(old[i] == new[c] for i, c in enumerate(cols)):
        return "unchanged"
    sets = ", ".join(f"{c} = ?" for c in [*cols, "git_commit"])
    cur.execute(f"UPDATE {table} SET {sets}, updated_at = SYSUTCDATETIME() WHERE {key_col} = ?",
                *[new[c] for c in cols], commit, key)
    return "updated"


def line(kind, name, status):
    return f"{kind:<7}{name} {'.' * max(2, 40 - len(name))} {status}"


def git_commit(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv[1:])

    errors, sources, tables = load_and_validate(DEFAULT_ROOT)
    if errors:
        for file, msg in errors:
            print(f"ERROR {file}:\n{msg}\n")
        print("Validation failed. Nothing was deployed.")
        return 1

    commit = git_commit(DEFAULT_ROOT)
    print(f"Deploying commit {commit}\n")

    if args.dry_run:
        for sid in sorted(sources):
            print(line("SOURCE", sid, "would be created/updated"))
        print()
        for tid in sorted(tables):
            print(line("OBJECT", tid, "would be created/updated"))
        return 0

    import pyodbc  # importeras här så att validering och tester inte kräver ODBC-drivrutin

    conn = pyodbc.connect(os.environ["METADATA_DB_CONNECTION_STRING"], autocommit=False)
    try:
        cur = conn.cursor()
        for sid in sorted(sources):
            status = upsert(cur, "metadata.Source", "source_id", sid, SOURCE_COLS,
                            source_row(sources[sid]), commit)
            print(line("SOURCE", sid, status))
        print()
        for tid in sorted(tables):
            status = upsert(cur, "metadata.IngestionObject", "object_id", tid, OBJECT_COLS,
                            object_row(tables[tid]), commit)
            print(line("OBJECT", tid, status))

        cur.execute("SELECT source_id FROM metadata.Source")
        for (sid,) in cur.fetchall():
            if sid not in sources:
                print(f"\nWARNING:\n{sid} exists in registry but not in Git.\nIt has NOT been deleted.")
        cur.execute("SELECT object_id FROM metadata.IngestionObject")
        for (oid,) in cur.fetchall():
            if oid not in tables:
                print(f"\nWARNING:\n{oid} exists in registry but not in Git.\nIt has NOT been deleted.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
