# git-meta-ingestion

Git beskriver. CI verifierar. SQL publicerar. ADF kör.

## 1. Arkitektur

```
sources/*.yml ┐
tables/**/*.yml ├─ validate.py ─ (merge till main) ─ deploy.py ─▶ Azure SQL ◀── ADF läser
sql/**/*.sql  ┘                                                  metadata.*   runtime.* skrivs av ADF
```

| Plats | Innehåll |
|---|---|
| `sources/<source>.yml` | En källa: id, typ, logisk `connection.ref` |
| `tables/<source>/<tabell>.yml` | Ett ingestion-objekt: vad som hämtas, hur, med vilka nycklar |
| `sql/<source>/<tabell>.sql` | Extraktionsfrågan. Refereras från tabell-YAML, dupliceras aldrig |
| `schemas/*.json` | JSON Schema för YAML-filerna |
| `src/validate.py` | Validerar hela repot |
| `src/deploy.py` | Upsertar till Azure SQL |
| `database/create_metadata_tables.sql` | Skapar registret (körs en gång, kan köras om) |

**`metadata.Source` och `metadata.IngestionObject` ägs av deploy-processen.** Skriv aldrig manuella INSERT/UPDATE där. Ändringen görs i Git och publiceras av pipelinen, annars skrivs den över vid nästa deploy.

## 2. Ny source

1. Skapa `sources/my_source.yml`:
   ```yaml
   id: my_source          # stabil identitet, ändra aldrig
   type: sql_server
   connection:
     ref: my-source-sql   # logiskt namn, inga lösenord här
   defaults:
     timeout: 3600
   ```
2. Skapa connection mapping utanför repot (t.ex. ADF Linked Service eller Key Vault-secret med namnet `my-source-sql`).
3. Skapa PR.

## 3. Ny table

1. `tables/fortnox/my_table.yml`:
   ```yaml
   id: fortnox.my_table     # stabil identitet, måste börja med "<source>."
   source: fortnox
   schema: dbo
   table: MyTable
   query: sql/fortnox/my_table.sql
   load:
     type: incremental      # full | incremental
     watermark: ModifiedDate  # krävs för incremental
   keys:
     - MyTableId
   enabled: true
   ```
2. `sql/fortnox/my_table.sql`. Vid `incremental` ska frågan filtrera på `@watermark`:
   ```sql
   SELECT MyTableId, Name, ModifiedDate
   FROM dbo.MyTable
   WHERE ModifiedDate > @watermark
   ```
3. Skapa PR.

Ändras `schema`, `table` eller `query` men inte `id` uppdateras samma rad i registret.

## 4. Validera lokalt

```
pip install -r requirements.txt
python src/validate.py      # exit code 1 vid fel
python -m pytest
python src/deploy.py --dry-run   # visar vad som skulle publiceras, ingen databas behövs
```

Exempel på felutskrift:

```
ERROR tables/fortnox/invoice.yml:
load.type is "incremental" but load.watermark is missing
```

Validatorn kontrollerar: giltig YAML, JSON Schema, unika `source.id` och `table.id`, att `table.source` finns, att `table.id` börjar med `<source>.`, att query-filen finns och inte bara är kommentarer, att `incremental` har `watermark` och använder `@watermark` i SQL, att `keys` finns utan dubbletter, att `enabled` är boolean.

## 5. Vad CI gör

`.github/workflows/ci.yml`:

```
feature branch → Pull Request → pytest + validate.py → review → main → validate.py → deploy.py → Azure SQL
```

PR kör bara validering. Deploy körs endast vid push till `main` och kräver secret `METADATA_DB_CONNECTION_STRING` (ODBC connection string, t.ex. med managed identity eller en SQL-användare med skrivrätt på `metadata`-schemat).

## 6. Deploy

Engångssetup: kör `database/create_metadata_tables.sql` mot Azure SQL.

`python src/deploy.py` validerar, läser all YAML + SQL och upsertar på `source.id` / `table.id`:

```
SOURCE fortnox ................................. unchanged

OBJECT fortnox.customer ........................ updated
OBJECT fortnox.invoice ......................... created
```

* Idempotent: samma Git-state två gånger ger `unchanged` och inga dubbletter.
* `git_commit` uppdateras bara på rader som faktiskt ändrats, och visar därför commit som senast ändrade just den raden.
* Allt körs i en transaktion.
* Deploy raderar aldrig. Finns ett objekt i registret men inte i Git skrivs en varning:
  ```
  WARNING:
  fortnox.legacy_invoice exists in registry but not in Git.
  It has NOT been deleted.
  ```

## 7. Disable

Sätt `enabled: false` i tabell-YAML och skapa PR. ADF filtrerar på `enabled = 1`.

## 8. Rollback

`git revert <commit>`, PR, merge. Pipelinen validerar och deployar, och registret får samma innehåll som före den felaktiga ändringen. Ett objekt som skapades av den reverterade commiten finns kvar i registret (inget DELETE), så sätt `enabled: false` för det i stället för att ta bort filen.

## 9. Static metadata vs runtime state

| | `metadata.*` | `runtime.*` |
|---|---|---|
| Innehåll | Konfiguration från Git | Senaste watermark, status, tidpunkt |
| Skrivs av | `deploy.py` | ADF / framtida Python-motor |
| Ändras genom | PR | Körningar |

`deploy.py` läser och skriver aldrig `runtime.IngestionState`, så en deploy kan inte nollställa ett watermark.

## Tabellmodell

```
metadata.Source           source_id PK, source_type, connection_ref, config_json, git_commit, updated_at
metadata.IngestionObject  object_id PK, source_id FK, source_schema, source_table, query_text,
                          load_type, watermark_column, keys_json, enabled, git_commit, updated_at
runtime.IngestionState    object_id PK/FK, last_successful_watermark, last_run_at, last_status
```

Fortnox-exemplet blir:

| YAML | Kolumn |
|---|---|
| `sources/fortnox.yml` `id` / `type` / `connection.ref` / `defaults` | `Source.source_id` = `fortnox` / `source_type` = `sql_server` / `connection_ref` = `fortnox-sql` / `config_json` = `{"defaults": {"timeout": 3600}}` |
| `tables/fortnox/invoice.yml` `id` | `IngestionObject.object_id` = `fortnox.invoice` |
| `source` / `schema` / `table` | `source_id` / `source_schema` = `dbo` / `source_table` = `Invoice` |
| innehållet i `sql/fortnox/invoice.sql` | `query_text` |
| `load.type` / `load.watermark` | `load_type` = `incremental` / `watermark_column` = `ModifiedDate` |
| `keys` | `keys_json` = `["InvoiceId"]` |
| `enabled` | `enabled` = 1 |
| `customer.yml` (`load.type: full`) | `load_type` = `full`, `watermark_column` = NULL |

## Kontrakt mot ADF

ADF behöver bara läsa registret. Formatet innehåller inga ADF-begrepp, så en Python-motor kan ersätta ADF utan att YAML ändras.

1. **Lookup** objekt: `SELECT o.object_id, s.connection_ref, o.query_text, o.load_type, o.keys_json, o.watermark_column FROM metadata.IngestionObject o JOIN metadata.Source s ON s.source_id = o.source_id WHERE o.enabled = 1`
2. **ForEach** över resultatet.
3. För `incremental`: `SELECT last_successful_watermark FROM runtime.IngestionState WHERE object_id = @object_id`. Saknas raden är det första körningen och motorn väljer ett startvärde (t.ex. `1900-01-01`).
4. Kör `query_text` mot källan som `connection_ref` pekar på, med `@watermark` ersatt av värdet från steg 3.
5. Vid lyckad körning: upserta `runtime.IngestionState` (`last_successful_watermark` = högsta `watermark_column` i det lästa datat, `last_run_at`, `last_status = 'succeeded'`). Vid fel skrivs bara `last_status = 'failed'`; watermark lämnas orört.

`connection_ref` mappas till en Linked Service / Key Vault utanför detta repo.

## Begränsningar i V1

* Ingen automatisk radering ur registret, bara varning.
* SQL valideras bara för att filen inte är tom och att `@watermark` finns vid incremental. Ingen SQL-parser.
* `deploy.py` är testat med `--dry-run`, inte mot en riktig Azure SQL.
* Bara `sql_server` som source-typ.
