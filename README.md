# Oracle DB unloader

Unload Oracle schema objects to a stable folder tree of SQL files. The tool
talks to Oracle through SQLAlchemy and `oracledb` (thin mode). Object DDL still
comes from packaged SQL scripts; Python lists objects, binds `:schema` /
`:obj_name` / `:obj_type`, writes files, and tracks incremental state.

Pass in a SQLAlchemy engine you already own. The
unloader does not dispose it.

## Install

```bash
pip install -e ".[test]"
```

## CLI

```bash
db-unloader --db-url oracle+oracledb://system:pass@localhost:1521/?service_name=FREEPDB1 \
  --schemas scott,hr \
  --object-types table,view,package \
  --output-dir ./ddl \
  --max-workers 5 \
  --dependencies
```

Login, password, and a TNS alias are also accepted:

```bash
db-unloader --db-login system --db-pass pass --db-tns orclpdb \
  --schemas scott --output-dir ./ddl
```

`--object-types` limits the run to selected Oracle types (`table`, `view`,
`package`, `mview`, `function`, `procedure`, `trigger`, `type`, `sequence`,
`synonym`). `package` and `type` include spec and body. Incremental watermarks
are stored per schema and object type as the max ``last_ddl_time`` actually
seen in Oracle. For tables and materialized views that value is
``greatest`` of the object, its partitions/subpartitions, and its indexes.
The next run writes objects whose ``last_ddl_time`` is greater than or
equal to that watermark. `--full` ignores watermarks and rewrites every
selected object. `--lookback-minutes` (default 0) can widen the cutoff.
By default any object exception stops the whole run: remaining types and
schemas are not started, and the failed type's watermark is not moved.
`--continue-on-error` keeps going through remaining objects, types, and
schemas; failed types still do not move their watermark, and the process
exits non-zero if anything failed. `--skip-objects SCHEMA.NAME,SCHEMA.TYPE.NAME`
omits specific objects (type is optional; `package body` and `package_body`
are the same). `--log-level DEBUG|INFO|WARNING|ERROR` (default `INFO`)
configures the CLI logger. Importing the library does not attach handlers;
the host process owns logging.

Functions, procedures, packages, types, and triggers are read from
`dba_source` and concatenated on the server as a CLOB, so a UTF-8 character
split across Oracle's 4000-byte source wrap is rejoined before it reaches
the client. If that still fails, the unloader falls back to
`dbms_metadata.get_ddl`.

Quoted mixed-case names such as `"MixedCase_metrics"` are passed to
`dbms_metadata.get_ddl` as stored in the dictionary (not uppercased). File
names stay lowercase (`mixedcase_metrics.sql`). Simple uppercase identifiers
are unquoted and lowercased; mixed-case and other names that need quoting
stay quoted. SQL statements end with `;`; PL/SQL source still uses `/`.

Custom SELECTs live in the `_meta/` folder at the output root, for example
`_meta/get_table_stats.sql`. Bind `:schema` to run the same query for every
schema. A script in `<schema>/_meta/*.sql` runs only for that schema (and
overrides a shared `_meta` script of the same name). Results are written as
`<schema>/_meta/<script>.md`.

`--dependencies` writes per-object leaf paths under `<schema>/_deps/<type>/`
with `## Downstream` (sources, path starts with `>`) and `## Upstream`
(consumers, path ends with `>`). The object's own name is omitted from
each path. That pass always refreshes the selected types: a new consumer
in another schema does not change the object's `last_ddl_time`. DDL
watermarks are not used. Off by default.

## Library

```python
from sqlalchemy import create_engine
from oracle_db_unloader import Unloader

engine = create_engine(
    'oracle+oracledb://system:pass@localhost:1521/?service_name=FREEPDB1',
    pool_size=5,
    max_overflow=0,
    pool_pre_ping=True,
)
try:
    client = Unloader(
        engine,
        schemas=['SCOTT'],
        object_types=['table', 'package'],
        output_dir='/data/ddl',
        max_workers=5,
        skip_objects=['SCOTT.BROKEN_FN'],
        continue_on_error=True,
    )
    client.ddl()
    client.dependencies()
    client.custom_sql()
finally:
    engine.dispose()
```

## Output layout

```
output_dir/
  _meta/get_table_stats.sql
  scott/.unloader_state.json
  scott/functions/*.sql
  scott/packages/*_s.sql *_b.sql
  scott/tables/*.sql
  scott/_deps/tables/*.md
  scott/_deps/views/*.md
  scott/_meta/get_table_stats.md
  scott/_meta/local.sql
  scott/_meta/local.md
  hr/.unloader_state.json
  hr/_meta/get_table_stats.md
  ...
```

The tree is reused across runs (no timestamp folder) so git and incremental
unloads work. Incremental watermarks live in each schema folder as
`.unloader_state.json`. Table and materialized-view files get an `alter table ... modify`
partition by ...` statement (template only, no per-partition lists). Custom
`_meta/` SELECT scripts are always re-run with `:schema`. Shared scripts write
under every schema; `<schema>/_meta/*.sql` writes only to that schema.

`--dependencies` writes `<schema>/_deps/<type>/<object>.md` with
`## Downstream` / `## Upstream` sections of leaf paths. Objects with no
edges produce no file; stale `_deps` files for the selected types are
removed.

## Tests

```bash
make test-unit
make up-db
make test-integration
make down-db
```

