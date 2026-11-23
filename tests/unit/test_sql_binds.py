from __future__ import annotations

from oracle_db_unloader.resources import (
    SQL_DIR,
    bind_params,
    ensure_semicolon,
    format_oracle_ident,
    format_oracle_ident_list,
    split_statements,
    strip_owner_qualifiers,
    unquote_simple_idents,
)

SQLPLUS_MARKERS = ('define ', 'spool ', '&1', '&v_', 'prompt ')


def test_unload_scripts_use_bind_variables():
    unload_files = sorted(SQL_DIR.glob('unload_*.sql'))
    assert unload_files
    for path in unload_files:
        sql = path.read_text(encoding='utf-8')
        lowered = sql.lower()
        for marker in SQLPLUS_MARKERS:
            assert marker not in lowered, path.name
        binds = bind_params(sql, {'schema': 'SCOTT', 'obj_name': 'EMP', 'obj_type': 'TABLE'})
        assert 'schema' in binds, path.name
        assert 'obj_name' in binds, path.name
        assert 'upper(:obj_name)' not in sql, path.name
    source = (SQL_DIR / 'unload_source.sql').read_text(encoding='utf-8').lower()
    assert 'xmlagg' in source
    assert 'dba_source' in source
    metadata = (SQL_DIR / 'unload_source_ddl.sql').read_text(encoding='utf-8').lower()
    assert 'dbms_metadata.get_ddl' in metadata


def test_inventory_tracks_partitions_and_indexes():
    inventory = (SQL_DIR / 'inventory.sql').read_text(encoding='utf-8')
    assert ':schema' in inventory
    assert 'TABLE PARTITION' in inventory
    assert 'dba_indexes' in inventory
    transform = (SQL_DIR / 'set_transform_params.sql').read_text(encoding='utf-8')
    assert 'EMIT_SCHEMA' in transform


def test_dependency_edges_are_uncycled_catalog_dump():
    sql = (SQL_DIR / 'dependency_edges.sql').read_text(encoding='utf-8')
    lowered = sql.lower()
    assert 'dba_dependencies' in lowered
    assert "referenced_type != 'non-existent'" in lowered
    assert "'sys'" in lowered
    assert ':schema' not in sql
    for marker in SQLPLUS_MARKERS:
        assert marker not in lowered


def test_split_statements_on_slash():
    statements = split_statements(
        'select :schema from dual\n/\nselect :obj_name from dual\n'
    )
    assert len(statements) == 2


def test_ensure_semicolon():
    assert ensure_semicolon('create table emp') == 'create table emp;\n'
    assert ensure_semicolon('end;\n/\n') == 'end;\n/\n'


def test_format_oracle_ident():
    assert format_oracle_ident('EMP_HEAP') == 'emp_heap'
    assert format_oracle_ident('MixedCase_metrics') == '"MixedCase_metrics"'
    assert format_oracle_ident_list('SALE_DATE,SOURCE_ID') == 'sale_date, source_id'
    assert format_oracle_ident_list('createdAt') == '"createdAt"'


def test_unquote_simple_idents():
    ddl = 'CREATE TABLE "EMP_HEAP" (\n  "ID" NUMBER,\n  "NAME" VARCHAR2(32)\n)\n'
    assert unquote_simple_idents(ddl) == (
        'CREATE TABLE emp_heap (\n  id NUMBER,\n  name VARCHAR2(32)\n)\n'
    )
    mixed = 'CREATE TABLE "MixedCase_metrics" (\n  "ID" NUMBER,\n  "createdAt" VARCHAR2(32)\n)\n'
    assert unquote_simple_idents(mixed) == (
        'CREATE TABLE "MixedCase_metrics" (\n  id NUMBER,\n  "createdAt" VARCHAR2(32)\n)\n'
    )
    comment = "comment on table \"EMP_HEAP\" is 'see \"FOO\" and \"MixedCase_metrics\"';"
    assert unquote_simple_idents(comment) == (
        "comment on table emp_heap is 'see \"FOO\" and \"MixedCase_metrics\"';"
    )


def test_strip_owner_qualifiers():
    ddl = 'create table "SCOTT"."EMP" (\n  id number\n)\n'
    stripped = strip_owner_qualifiers(ddl, 'SCOTT')
    assert stripped == 'create table "EMP" (\n  id number\n)\n'
    assert unquote_simple_idents(stripped) == 'create table emp (\n  id number\n)\n'
