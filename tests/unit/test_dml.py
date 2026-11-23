from __future__ import annotations

import logging
import threading
from datetime import datetime
from decimal import Decimal

import pytest

from oracle_db_unloader.dml import (
    discover_custom_sql,
    dump_custom_sql,
    format_table,
    stringify_cell,
)


def test_github_table_renders_nulls():
    rendered = format_table(['ID', 'NAME'], [[1, 'abc'], [None, 'x']])
    assert 'abc' in rendered
    assert 'null' in rendered


def test_stringify_cell_formats_types():
    assert stringify_cell(None) is None
    assert stringify_cell(Decimal('10.50')) == '10.5'
    assert stringify_cell(datetime(2026, 11, 16, 8, 0, 0)) == '16-nov-2026 08:00:00'


def test_discover_custom_sql_meta_only(tmp_path):
    meta = tmp_path / '_meta'
    other = tmp_path / '_stats'
    meta.mkdir()
    other.mkdir()
    (meta / 'get_table_stats.sql').write_text(
        'select :schema as owner from dual',
        encoding='utf-8',
    )
    (other / 'ignored.sql').write_text('select 1 from dual', encoding='utf-8')
    assert discover_custom_sql(tmp_path) == [meta / 'get_table_stats.sql']


def test_dump_custom_sql_writes_per_schema(tmp_path):
    meta = tmp_path / '_meta'
    meta.mkdir()
    (meta / 'get_table_stats.sql').write_text(
        'select owner from dba_tables where owner = :schema',
        encoding='utf-8',
    )
    conn = _FakeConnection(
        columns=['OWNER'],
        rows_by_schema={'SCOTT': [['SCOTT']], 'HR': [['HR']]},
    )
    dumped = dump_custom_sql(_FakeEngine(conn), tmp_path, ['scott', 'hr'])
    assert dumped == 2
    assert 'SCOTT' in (tmp_path / 'scott' / '_meta' / 'get_table_stats.md').read_text(
        encoding='utf-8'
    )
    assert 'HR' in (tmp_path / 'hr' / '_meta' / 'get_table_stats.md').read_text(
        encoding='utf-8'
    )


def test_dump_schema_specific_meta_sql_only_that_schema(tmp_path):
    scott_meta = tmp_path / 'scott' / '_meta'
    scott_meta.mkdir(parents=True)
    (scott_meta / 'local.sql').write_text(
        'select :schema as owner from dual',
        encoding='utf-8',
    )
    conn = _FakeConnection(
        columns=['OWNER'],
        rows_by_schema={'SCOTT': [['SCOTT']], 'HR': [['HR']]},
    )
    dumped = dump_custom_sql(_FakeEngine(conn), tmp_path, ['SCOTT', 'HR'])
    assert dumped == 1
    assert (tmp_path / 'scott' / '_meta' / 'local.md').is_file()
    assert not (tmp_path / 'hr' / '_meta' / 'local.md').exists()


def test_schema_meta_overrides_shared_script(tmp_path):
    shared = tmp_path / '_meta'
    shared.mkdir()
    (shared / 'stats.sql').write_text(
        "select 'shared' as src, :schema as owner from dual",
        encoding='utf-8',
    )
    scott_meta = tmp_path / 'scott' / '_meta'
    scott_meta.mkdir(parents=True)
    (scott_meta / 'stats.sql').write_text(
        "select 'local' as src, :schema as owner from dual",
        encoding='utf-8',
    )
    conn = _FakeConnection(
        columns=['SRC', 'OWNER'],
        rows_by_schema={
            'SCOTT': [['local', 'SCOTT']],
            'HR': [['shared', 'HR']],
        },
    )
    dumped = dump_custom_sql(_FakeEngine(conn), tmp_path, ['SCOTT', 'HR'])
    assert dumped == 2
    sql_by_schema = {params['schema']: sql for sql, params in conn.calls}
    assert 'local' in sql_by_schema['SCOTT']
    assert 'shared' in sql_by_schema['HR']


def test_dump_custom_sql_logs_workers(tmp_path, caplog):
    meta = tmp_path / '_meta'
    meta.mkdir()
    (meta / 'get_table_stats.sql').write_text(
        'select :schema as owner from dual',
        encoding='utf-8',
    )
    conn = _FakeConnection(
        columns=['OWNER'],
        rows_by_schema={'SCOTT': [['SCOTT']], 'HR': [['HR']]},
    )
    caplog.set_level(logging.INFO, logger='oracle_db_unloader')
    dumped = dump_custom_sql(
        _FakeEngine(conn),
        tmp_path,
        ['SCOTT', 'HR'],
        max_workers=5,
    )
    assert dumped == 2
    assert (
        'custom sql: 1 shared script(s), 0 schema-specific for 2 schema(s) '
        'jobs=2 workers=2'
    ) in caplog.text


def test_dump_custom_sql_failure_stops_remaining(monkeypatch, tmp_path):
    meta = tmp_path / '_meta'
    meta.mkdir()
    (meta / 'stats.sql').write_text(
        'select :schema as owner from dual',
        encoding='utf-8',
    )
    ran = []

    def fake_run(_engine, job):
        ran.append(job.schema)
        if job.schema == 'SCOTT':
            raise RuntimeError('boom')
        return 1

    monkeypatch.setattr('oracle_db_unloader.dml._run_custom_sql', fake_run)
    with pytest.raises(RuntimeError, match='stats.sql schema=SCOTT'):
        dump_custom_sql(
            _FakeEngine(_FakeConnection(columns=['OWNER'], rows_by_schema={})),
            tmp_path,
            ['SCOTT', 'HR'],
            max_workers=1,
        )
    assert ran == ['SCOTT']


class _FakeResult:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def keys(self):
        return self._columns

    def __iter__(self):
        return iter(self._rows)


class _FakeConnection:
    def __init__(self, columns, rows_by_schema):
        self.columns = columns
        self.rows_by_schema = rows_by_schema
        self.calls = []
        self._lock = threading.Lock()

    def execute(self, statement, params=None):
        sql = getattr(statement, 'text', None) or str(statement)
        with self._lock:
            self.calls.append((sql, params or {}))
            schema = (params or {}).get('schema')
            rows = list(self.rows_by_schema.get(schema, []))
        return _FakeResult(self.columns, rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def connect(self):
        return self.conn
