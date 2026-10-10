import sqlite3
from pathlib import Path

import pytest

import deploy
from validate import DEFAULT_ROOT, load_and_validate


class Cursor:
    """Ger sqlite3 pyodbc:s varargs-stil, så att upsert() kan testas utan SQL Server."""

    def __init__(self, conn):
        self.c = conn.cursor()

    def execute(self, sql, *args):
        self.c.execute(sql, args)

    def fetchone(self):
        return self.c.fetchone()


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.create_function("SYSUTCDATETIME", 0, lambda: "now")
    conn.execute("CREATE TABLE src (source_id, source_type, connection_ref, config_json,"
                 " git_commit, updated_at)")
    return conn, Cursor(conn)


def test_upsert_created_unchanged_updated(db):
    conn, cur = db
    _, sources, _ = load_and_validate(DEFAULT_ROOT)
    row = deploy.source_row(sources["fortnox"])

    assert deploy.upsert(cur, "src", "source_id", "fortnox", row, "aaa") == "created"
    assert deploy.upsert(cur, "src", "source_id", "fortnox", row, "bbb") == "unchanged"
    # unchanged rör inte raden, så git_commit visar senaste commit som faktiskt ändrade den
    assert conn.execute("SELECT git_commit FROM src").fetchone() == ("aaa",)

    row["connection_ref"] = "other"
    assert deploy.upsert(cur, "src", "source_id", "fortnox", row, "ccc") == "updated"
    assert conn.execute("SELECT connection_ref, git_commit FROM src").fetchall() == [("other", "ccc")]
    assert conn.execute("SELECT count(*) FROM src").fetchone() == (1,)


def test_split_batches_on_go():
    sql = "SELECT 1;\nGO\n\nSELECT 2;\n  go  \nSELECT 3;\n"
    assert deploy.split_batches(sql) == ["SELECT 1;", "SELECT 2;", "SELECT 3;"]


def test_schema_file_splits_into_batches():
    batches = deploy.split_batches((DEFAULT_ROOT / "database/schema.sql").read_text())
    assert len(batches) >= 4
    assert not any(b.upper() == "GO" for b in batches)


def test_deploy_never_writes_to_runtime_state():
    code = Path(deploy.__file__).read_text()
    assert "IngestionState" not in code
