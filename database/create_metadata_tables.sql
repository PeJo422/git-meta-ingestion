-- Skapar registret. Kan köras flera gånger.
--
-- metadata.*  ägs av src/deploy.py och är en publicerad kopia av Git. Ändra aldrig manuellt.
-- runtime.*   ägs av ingestion-motorn (ADF). deploy.py läser och skriver aldrig här.

IF SCHEMA_ID('metadata') IS NULL EXEC('CREATE SCHEMA metadata');
IF SCHEMA_ID('runtime')  IS NULL EXEC('CREATE SCHEMA runtime');
GO

IF OBJECT_ID('metadata.Source') IS NULL
CREATE TABLE metadata.Source (
    source_id      varchar(100)   NOT NULL,
    source_type    varchar(50)    NOT NULL,
    connection_ref varchar(200)   NOT NULL,
    config_json    nvarchar(max)  NOT NULL,   -- defaults m.m. från YAML
    git_commit     char(40)       NOT NULL,
    updated_at     datetime2(0)   NOT NULL CONSTRAINT DF_Source_updated_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_Source PRIMARY KEY (source_id)
);
GO

IF OBJECT_ID('metadata.IngestionObject') IS NULL
CREATE TABLE metadata.IngestionObject (
    object_id        varchar(200)  NOT NULL,
    source_id        varchar(100)  NOT NULL,
    source_schema    sysname       NOT NULL,
    source_table     sysname       NOT NULL,
    query_text       nvarchar(max) NOT NULL,
    load_type        varchar(20)   NOT NULL,
    watermark_column sysname       NULL,
    keys_json        nvarchar(max) NOT NULL,
    enabled          bit           NOT NULL,
    git_commit       char(40)      NOT NULL,
    updated_at       datetime2(0)  NOT NULL CONSTRAINT DF_IngestionObject_updated_at DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_IngestionObject PRIMARY KEY (object_id),
    CONSTRAINT FK_IngestionObject_Source FOREIGN KEY (source_id) REFERENCES metadata.Source (source_id),
    CONSTRAINT CK_IngestionObject_load_type CHECK (load_type IN ('full', 'incremental')),
    CONSTRAINT CK_IngestionObject_watermark CHECK (load_type = 'full' OR watermark_column IS NOT NULL)
);
GO

-- Raden skapas av motorn vid första lyckade körning. deploy.py skapar eller ändrar den aldrig.
IF OBJECT_ID('runtime.IngestionState') IS NULL
CREATE TABLE runtime.IngestionState (
    object_id                 varchar(200)  NOT NULL,
    last_successful_watermark nvarchar(100) NULL,
    last_run_at               datetime2(0)  NULL,
    last_status               varchar(20)   NULL,   -- t.ex. 'succeeded' / 'failed'
    CONSTRAINT PK_IngestionState PRIMARY KEY (object_id),
    CONSTRAINT FK_IngestionState_Object FOREIGN KEY (object_id) REFERENCES metadata.IngestionObject (object_id)
);
GO
