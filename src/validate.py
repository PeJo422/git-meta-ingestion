"""Validera all metadata i repositoryt. Exit code 1 vid fel.

    python src/validate.py [repo-root]

deploy.py återanvänder load_and_validate() så att samma regler gäller vid deploy.
"""
import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def _rel(path, root):
    return path.relative_to(root).as_posix()


def _read_yaml(path, root, errors):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        errors.append((_rel(path, root), f"invalid YAML: {e}".replace("\n", " ")))
        return None
    if not isinstance(data, dict):
        errors.append((_rel(path, root), "file must contain a YAML mapping"))
        return None
    return data


def _schema_errors(data, schema):
    out = []
    for e in sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path)):
        field = ".".join(str(p) for p in e.path)
        out.append(f"{field}: {e.message}" if field else e.message)
    return out


def _strip_sql_comments(sql):
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", sql).strip()


def load_and_validate(root):
    """Returnerar (errors, sources, tables).

    errors:  lista av (fil, meddelande)
    sources: {source_id: dict}
    tables:  {table_id: dict} där dict även har 'query_text' och '_file'
    """
    root = Path(root)
    errors = []
    sources, tables = {}, {}
    source_schema = json.loads((root / "schemas/source.schema.json").read_text())
    table_schema = json.loads((root / "schemas/table.schema.json").read_text())
    source_files = {}
    table_files = {}

    for path in sorted((root / "sources").glob("*.yml")):
        rel = _rel(path, root)
        data = _read_yaml(path, root, errors)
        if data is None:
            continue
        schema_errs = _schema_errors(data, source_schema)
        for msg in schema_errs:
            errors.append((rel, msg))
        if schema_errs:
            continue
        if data["id"] in sources:
            errors.append((rel, f'duplicate source id "{data["id"]}" (also in {source_files[data["id"]]})'))
            continue
        sources[data["id"]] = data
        source_files[data["id"]] = rel

    for path in sorted((root / "tables").glob("**/*.yml")):
        rel = _rel(path, root)
        data = _read_yaml(path, root, errors)
        if data is None:
            continue
        schema_errs = _schema_errors(data, table_schema)
        for msg in schema_errs:
            errors.append((rel, msg))
        if schema_errs:
            continue

        n_before = len(errors)
        load = data["load"]
        if load["type"] == "incremental" and not load.get("watermark"):
            errors.append((rel, 'load.type is "incremental" but load.watermark is missing'))
        if load["type"] == "full" and "watermark" in load:
            errors.append((rel, 'load.type is "full" but load.watermark is set'))

        if len(set(data["keys"])) != len(data["keys"]):
            dups = sorted({k for k in data["keys"] if data["keys"].count(k) > 1})
            errors.append((rel, f"keys contains duplicates: {', '.join(dups)}"))

        if data["source"] not in sources:
            errors.append((rel, f'source "{data["source"]}" does not exist in sources/'))
        elif not data["id"].startswith(data["source"] + "."):
            errors.append((rel, f'id "{data["id"]}" must start with "{data["source"]}."'))

        query_path = root / data["query"]
        query_text = None
        if not query_path.is_file():
            errors.append((rel, f'query file "{data["query"]}" does not exist'))
        else:
            query_text = query_path.read_text(encoding="utf-8")
            body = _strip_sql_comments(query_text)
            if not body:
                errors.append((rel, f'query file "{data["query"]}" is empty'))
            elif load["type"] == "incremental" and "@watermark" not in body:
                errors.append((rel, f'load.type is "incremental" but {data["query"]} does not use @watermark'))

        if data["id"] in tables:
            errors.append((rel, f'duplicate table id "{data["id"]}" (also in {table_files[data["id"]]})'))
            continue
        table_files[data["id"]] = rel
        if len(errors) == n_before:
            tables[data["id"]] = {**data, "query_text": query_text, "_file": rel}

    return errors, sources, tables


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else DEFAULT_ROOT
    errors, sources, tables = load_and_validate(root)
    for file, msg in errors:
        print(f"ERROR {file}:\n{msg}\n")
    if errors:
        print(f"Validation failed: {len(errors)} error(s).")
        return 1
    print(f"OK: {len(sources)} source(s), {len(tables)} table(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
