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
| `database/schema.sql` | Databasschemat. `deploy.py` kör filen vid varje deploy |

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

`python src/deploy.py` gör i ordning:

1. Validerar hela repot och avbryter vid fel.
2. Kör `database/schema.sql` (tål att köras om, skapar saknade tabeller och kolumner).
3. Läser all YAML + SQL och upsertar på `source.id` / `table.id`:

```
SOURCE fortnox ................................. unchanged

OBJECT fortnox.customer ........................ updated
OBJECT fortnox.invoice ......................... created
```

* Idempotent: samma Git-state två gånger ger `unchanged` och inga dubbletter.
* `git_commit` uppdateras bara på rader som faktiskt ändrats, och visar därför commit som senast ändrade just den raden.
* Schema och upsert körs i samma transaktion. En deploy som misslyckas lämnar databasen som den var.
* Två deployer körs aldrig samtidigt (`concurrency` i CI).
* Deploy raderar aldrig. Finns ett objekt i registret men inte i Git skrivs en varning:
  ```
  WARNING:
  fortnox.legacy_invoice exists in registry but not in Git.
  It has NOT been deleted.
  ```

## 7. Disable

Sätt `enabled: false` i tabell-YAML och skapa PR. ADF filtrerar på `enabled = 1`.

## 8. Ändra tabellstrukturen (ny kolumn)

Gäller när `metadata.*` ska få en ny kolumn, till exempel `page_size` på `IngestionObject`. Allt görs i **en** PR:

1. `schemas/table.schema.json`: lägg fältet. Schemat tillåter inga okända fält, så validatorn avvisar det annars.
2. `database/schema.sql`: lägg ett ALTER-block **längst ner**, och rör inte den befintliga `CREATE TABLE`:
   ```sql
   IF COL_LENGTH('metadata.IngestionObject', 'page_size') IS NULL
       ALTER TABLE metadata.IngestionObject ADD page_size int NULL;
   GO
   ```
   Kolumnen ska vara `NULL` eller ha `DEFAULT`, eftersom raderna redan finns. Nya och gamla miljöer kör samma ALTER-block och får därför samma schema.
3. `src/deploy.py`: lägg `"page_size": t.get("page_size")` i `object_row()` (eller `source_row()`). Det är den enda platsen kolumner anges.
4. Valfritt: valideringsregel i `validate.py`, exempel i en YAML-fil, och en rad i README-tabellen över YAML→kolumn.
5. Efter merge: uppdatera ADF-lookupen att hämta kolumnen. Kolumnen är nullable och bryter därför inte ADF innan dess.

Pipelinen kör schemat före upserten i samma transaktion, så koden kan aldrig köras mot en databas som saknar kolumnen.

Byta namn på eller ta bort en kolumn görs i två PR:er. Först läggs den nya till och konsumenterna flyttas, och i en senare PR tas den gamla bort med en egen `ALTER TABLE ... DROP COLUMN`-rad. Annars slutar ADF fungera mellan deploy och pipelineändring.

Valfria inställningar som bara en källtyp använder kan ligga under `defaults` i source-YAML. De sparas i `config_json` och kräver ingen ändring av schemat.

**Behörighet:** deploy-identiteten behöver rätt att skapa och ändra tabeller i `metadata`-schemat, och skriva (inte skapa) i `runtime`. Ger du den inte DDL-rätt får du köra `schema.sql` manuellt före merge, men då är inte längre allt automatiserat.

## 9. Rollback

`git revert <commit>`, PR, merge. Pipelinen validerar och deployar, och registret får samma innehåll som före den felaktiga ändringen. Ett objekt som skapades av den reverterade commiten finns kvar i registret (inget DELETE), så sätt `enabled: false` för det i stället för att ta bort filen. Schemaändringar i `schema.sql` återställs inte av en revert (kolumner finns kvar), vilket är avsiktligt: en extra nullable kolumn skadar inte, medan en borttagen kolumn kan göra det.

## 10. Static metadata vs runtime state

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
* `deploy.py` är testat med `--dry-run` och med upsert-logiken mot SQLite, inte mot en riktig Azure SQL. Kör första deployen mot en testdatabas.
* Det finns ingen migrationsmotor. `schema.sql` är en idempotent fil med ALTER-block, och kolumner tas bort för hand i en egen PR.
* Bara `sql_server` som source-typ.
