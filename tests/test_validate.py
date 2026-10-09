import shutil
from pathlib import Path

import pytest

from validate import DEFAULT_ROOT, load_and_validate


@pytest.fixture
def repo(tmp_path):
    """En kopia av exempel-repot som varje test får förstöra."""
    for d in ("schemas", "sources", "tables", "sql"):
        shutil.copytree(DEFAULT_ROOT / d, tmp_path / d)
    return tmp_path


def messages(repo):
    errors, _, _ = load_and_validate(repo)
    return [f"{f}: {m}" for f, m in errors]


def edit(path, old, new):
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_valid_source_and_tables(repo):
    errors, sources, tables = load_and_validate(repo)
    assert errors == []
    assert set(sources) == {"fortnox"}
    assert set(tables) == {"fortnox.customer", "fortnox.invoice"}
    assert "@watermark" in tables["fortnox.invoice"]["query_text"]


def test_duplicate_source_id(repo):
    shutil.copy(repo / "sources/fortnox.yml", repo / "sources/fortnox_copy.yml")
    assert any('duplicate source id "fortnox"' in m for m in messages(repo))


def test_duplicate_table_id(repo):
    shutil.copy(repo / "tables/fortnox/invoice.yml", repo / "tables/fortnox/invoice_copy.yml")
    assert any('duplicate table id "fortnox.invoice"' in m for m in messages(repo))


def test_unknown_source(repo):
    edit(repo / "tables/fortnox/invoice.yml", "source: fortnox", "source: nope")
    assert any('source "nope" does not exist' in m for m in messages(repo))


def test_missing_sql_file(repo):
    (repo / "sql/fortnox/invoice.sql").unlink()
    assert any("does not exist" in m and "invoice.sql" in m for m in messages(repo))


def test_empty_sql_file(repo):
    (repo / "sql/fortnox/customer.sql").write_text("-- bara en kommentar\n")
    assert any("is empty" in m for m in messages(repo))


def test_incremental_query_without_watermark_parameter(repo):
    (repo / "sql/fortnox/invoice.sql").write_text("SELECT InvoiceId FROM dbo.Invoice")
    assert any("does not use @watermark" in m for m in messages(repo))


def test_incremental_without_watermark(repo):
    edit(repo / "tables/fortnox/invoice.yml", "  watermark: ModifiedDate\n", "")
    assert ('tables/fortnox/invoice.yml: load.type is "incremental" '
            'but load.watermark is missing') in messages(repo)


def test_invalid_load_type(repo):
    edit(repo / "tables/fortnox/invoice.yml", "type: incremental", "type: cdc")
    assert any("load.type" in m for m in messages(repo))


def test_duplicate_keys(repo):
    edit(repo / "tables/fortnox/invoice.yml", "  - InvoiceId", "  - InvoiceId\n  - InvoiceId")
    assert any("keys contains duplicates: InvoiceId" in m for m in messages(repo))


def test_enabled_must_be_boolean(repo):
    edit(repo / "tables/fortnox/invoice.yml", "enabled: true", 'enabled: "yes"')
    assert any(m.startswith("tables/fortnox/invoice.yml: enabled") for m in messages(repo))


def test_invalid_yaml(repo):
    (repo / "sources/broken.yml").write_text("id: [unclosed\n")
    assert any("invalid YAML" in m for m in messages(repo))
