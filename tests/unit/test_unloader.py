from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from oracle_db_unloader.inventory import DbObject
from oracle_db_unloader.state import get_last_unload_at, load_state, state_path
from oracle_db_unloader.unloader import UnloadError, Unloader, fetch_object_ddl


def _obj(schema, name, object_type='TABLE', when=None):
    return DbObject(
        schema=schema,
        owner=schema,
        name=name,
        object_type=object_type,
        last_ddl_time=when or datetime(2026, 11, 16, 8, 0, 0),
    )


def _ddl(tmp_path, object_types, schemas=None, full=False, lookback_minutes=0, **init_kwargs):
    Unloader(
        MagicMock(),
        schemas or ['SCOTT'],
        tmp_path,
        object_types=object_types,
        max_workers=1,
        **init_kwargs,
    ).ddl(full=full, lookback_minutes=lookback_minutes)


def test_continue_on_error_defaults_to_false(tmp_path):
    client = Unloader(MagicMock(), ['SCOTT'], tmp_path, object_types=['table'])
    assert client.continue_on_error is False


def test_first_exception_stops_remaining_types_and_schemas(
    monkeypatch, tmp_path, caplog
):
    inventory = {
        'SCOTT': [_obj('SCOTT', 'EMP'), _obj('SCOTT', 'V_EMP', 'VIEW')],
        'HR': [_obj('HR', 'DEPT')],
    }
    written = []

    def fake_inventory(_engine, schema):
        return list(inventory[schema])

    def fake_write(_engine, obj, output_dir):
        written.append((obj.owner, obj.object_type, obj.name))
        if obj.owner == 'SCOTT' and obj.object_type == 'VIEW':
            raise RuntimeError('view boom')
        path = output_dir / obj.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('-- ok\n', encoding='utf-8')

    monkeypatch.setattr('oracle_db_unloader.unloader.fetch_inventory', fake_inventory)
    monkeypatch.setattr('oracle_db_unloader.unloader.write_object', fake_write)

    caplog.set_level(logging.INFO, logger='oracle_db_unloader')
    with pytest.raises(UnloadError, match='VIEW SCOTT.V_EMP'):
        _ddl(
            tmp_path,
            ['table', 'view'],
            schemas=['SCOTT', 'HR'],
            continue_on_error=False,
        )

    assert written == [('SCOTT', 'TABLE', 'EMP'), ('SCOTT', 'VIEW', 'V_EMP')]
    scott = load_state(tmp_path, 'SCOTT')
    assert get_last_unload_at(scott, 'TABLE') is not None
    assert get_last_unload_at(scott, 'VIEW') is None
    assert get_last_unload_at(load_state(tmp_path, 'HR'), 'TABLE') is None
    assert not (tmp_path / 'hr').exists()
    assert 'watermark not moved' not in caplog.text
    assert 'failed to unload 1 object' not in caplog.text


def test_same_type_failure_does_not_write_remaining(monkeypatch, tmp_path):
    written = []

    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: [_obj('SCOTT', 'T1'), _obj('SCOTT', 'T2')],
    )

    def fake_write(_engine, obj, output_dir):
        written.append(obj.name)
        if obj.name == 'T1':
            raise RuntimeError('boom')

    monkeypatch.setattr('oracle_db_unloader.unloader.write_object', fake_write)

    with pytest.raises(UnloadError, match='TABLE SCOTT.T1'):
        _ddl(tmp_path, ['table'], continue_on_error=False)
    assert written == ['T1']
    assert not (tmp_path / 'scott' / 'tables' / 't2.sql').exists()


def test_progress_and_summary_logging(monkeypatch, tmp_path, caplog):
    emp = _obj('SCOTT', 'EMP')
    gone = tmp_path / 'scott' / 'tables' / 'gone.sql'
    gone.parent.mkdir(parents=True)
    gone.write_text('-- gone\n', encoding='utf-8')

    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: [emp],
    )

    def fake_write(_engine, obj, output_dir):
        path = output_dir / obj.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('-- emp\n', encoding='utf-8')

    monkeypatch.setattr('oracle_db_unloader.unloader.write_object', fake_write)

    caplog.set_level(logging.INFO, logger='oracle_db_unloader')
    _ddl(tmp_path, ['table'])

    assert 'ddl complete added=1 updated=0 deleted=1' in caplog.text
    assert not gone.exists()


def test_empty_schema_writes_nothing(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: [],
    )
    caplog.set_level(logging.INFO, logger='oracle_db_unloader')
    _ddl(tmp_path, ['table'], schemas=['EMPTY'])
    assert not (tmp_path / 'empty').exists()
    assert not state_path(tmp_path, 'EMPTY').exists()
    assert 'inventory=0 write=0 delete=0' in caplog.text
    assert 'ddl complete added=0 updated=0 deleted=0' in caplog.text


def test_empty_schema_deletes_stale_files(monkeypatch, tmp_path):
    stale = tmp_path / 'empty' / 'tables' / 'gone.sql'
    stale.parent.mkdir(parents=True)
    stale.write_text('-- gone\n', encoding='utf-8')
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: [],
    )
    _ddl(tmp_path, ['table'], schemas=['EMPTY'])
    assert not stale.exists()
    assert get_last_unload_at(load_state(tmp_path, 'EMPTY'), 'TABLE') is None


def test_unloader_stages_are_independent(monkeypatch, tmp_path):
    called = []

    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: [_obj('SCOTT', 'EMP')],
    )

    def fake_write(_engine, obj, output_dir):
        called.append('ddl')
        path = output_dir / obj.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('-- emp\n', encoding='utf-8')

    monkeypatch.setattr('oracle_db_unloader.unloader.write_object', fake_write)
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.dump_dependencies',
        lambda *args, **kwargs: called.append('deps') or 1,
    )
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.dump_custom_sql',
        lambda *args, **kwargs: called.append('custom') or 0,
    )

    client = Unloader(
        MagicMock(),
        ['SCOTT'],
        tmp_path,
        object_types=['table'],
        max_workers=1,
    )
    client.dependencies()
    assert called == ['deps']
    client.custom_sql()
    assert called == ['deps', 'custom']
    client.ddl()
    assert called == ['deps', 'custom', 'ddl']
    assert (tmp_path / 'scott' / 'tables' / 'emp.sql').is_file()


def test_dependencies_pass_max_workers(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.dump_dependencies',
        lambda *args, **kwargs: seen.update(kwargs) or 1,
    )
    Unloader(MagicMock(), ['SCOTT'], tmp_path, max_workers=4).dependencies()
    assert seen['max_workers'] == 4


def test_continue_on_error_writes_remaining_and_later_types(
    monkeypatch, tmp_path, caplog
):
    written = []
    inventory = [
        _obj('SCOTT', 'T1'),
        _obj('SCOTT', 'T2'),
        _obj('SCOTT', 'V_EMP', 'VIEW'),
    ]

    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: list(inventory),
    )

    def fake_write(_engine, obj, output_dir):
        written.append((obj.object_type, obj.name))
        if obj.name == 'T1':
            raise RuntimeError('boom')
        path = output_dir / obj.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('-- ok\n', encoding='utf-8')

    monkeypatch.setattr('oracle_db_unloader.unloader.write_object', fake_write)
    caplog.set_level(logging.INFO, logger='oracle_db_unloader')

    with pytest.raises(UnloadError, match='failed to unload 1 object'):
        _ddl(tmp_path, ['table', 'view'], continue_on_error=True)

    assert written == [('TABLE', 'T1'), ('TABLE', 'T2'), ('VIEW', 'V_EMP')]
    assert (tmp_path / 'scott' / 'tables' / 't2.sql').is_file()
    assert (tmp_path / 'scott' / 'views' / 'v_emp.sql').is_file()
    assert not (tmp_path / 'scott' / 'tables' / 't1.sql').exists()
    scott = load_state(tmp_path, 'SCOTT')
    assert get_last_unload_at(scott, 'TABLE') is None
    assert get_last_unload_at(scott, 'VIEW') is not None
    assert 'watermark not moved' in caplog.text


def test_skip_objects_are_not_written(monkeypatch, tmp_path, caplog):
    written = []
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.fetch_inventory',
        lambda _engine, _schema: [
            _obj('SCOTT', 'T1'),
            _obj('SCOTT', 'T2'),
            _obj('SCOTT', 'FN', 'FUNCTION'),
        ],
    )

    def fake_write(_engine, obj, output_dir):
        written.append(obj.name)
        path = output_dir / obj.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('-- ok\n', encoding='utf-8')

    monkeypatch.setattr('oracle_db_unloader.unloader.write_object', fake_write)
    caplog.set_level(logging.WARNING, logger='oracle_db_unloader')
    _ddl(
        tmp_path,
        ['table', 'function'],
        skip_objects=['SCOTT.T1', 'scott.function.fn'],
    )
    assert written == ['T2']
    assert 'skipped TABLE SCOTT.T1' in caplog.text
    assert 'skipped FUNCTION SCOTT.FN' in caplog.text
    assert get_last_unload_at(load_state(tmp_path, 'SCOTT'), 'TABLE') is not None


def test_fetch_object_ddl_falls_back_to_get_ddl():
    obj = _obj('SCOTT', 'PKG', 'PACKAGE BODY')
    conn = MagicMock()
    calls = []

    def execute(_stmt, params):
        calls.append(dict(params))
        if len(calls) == 1:
            raise UnicodeDecodeError(
                'utf-8', b'\xd1', 3999, 4000, 'unexpected end of data'
            )
        rows = MagicMock()
        rows.returns_rows = True
        rows.__iter__.return_value = iter(
            [('CREATE OR REPLACE PACKAGE BODY pkg\nEND;\n/\n',)]
        )
        return rows

    conn.execute.side_effect = execute
    ddl = fetch_object_ddl(conn, obj)
    assert 'package body pkg' in ddl.lower()
    assert calls[0]['obj_type'] == 'PACKAGE BODY'
    assert calls[1]['obj_type'] == 'PACKAGE_BODY'


def test_custom_sql_pass_max_workers(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        'oracle_db_unloader.unloader.dump_custom_sql',
        lambda *args, **kwargs: seen.update(kwargs) or 0,
    )
    Unloader(MagicMock(), ['SCOTT'], tmp_path, max_workers=4).custom_sql()
    assert seen['max_workers'] == 4
