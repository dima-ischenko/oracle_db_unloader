from __future__ import annotations

from datetime import datetime

import pytest

from oracle_db_unloader.inventory import (
    ALL_OBJECT_TYPES,
    DbObject,
    max_last_ddl_time,
    metadata_object_type,
    next_watermark,
    normalize_object_types,
    normalize_skip_objects,
    object_deps_relpath,
    object_is_skipped,
    object_relpath,
    plan_actions,
    walk_schema_files,
)


def _obj(name, object_type, last_ddl, schema='SCOTT'):
    return DbObject(
        schema=schema,
        owner=schema,
        name=name,
        object_type=object_type,
        last_ddl_time=last_ddl,
    )


def test_object_relpath_suffixes():
    assert object_relpath('SCOTT', 'EMP', 'TABLE') == 'scott/tables/emp.sql'
    assert object_relpath('SCOTT', 'PKG_X', 'PACKAGE') == 'scott/packages/pkg_x_s.sql'
    assert object_relpath('SCOTT', 'PKG_X', 'PACKAGE BODY') == 'scott/packages/pkg_x_b.sql'
    assert object_relpath('SCOTT', 'MixedCase_metrics', 'TABLE') == (
        'scott/tables/mixedcase_metrics.sql'
    )
    assert object_deps_relpath('SCOTT', 'EMP', 'TABLE') == 'scott/_deps/tables/emp.md'
    assert object_deps_relpath('SCOTT', 'PKG_X', 'PACKAGE BODY') == (
        'scott/_deps/packages/pkg_x_b.md'
    )


def test_plan_write_when_missing_or_changed():
    stamp = datetime(2026, 11, 16, 8, 0, 0)
    emp = _obj('EMP', 'TABLE', datetime(2026, 11, 16, 7, 0, 0))
    pkg = _obj('PKG_X', 'PACKAGE', datetime(2026, 11, 16, 8, 10, 0))
    view = _obj('V_EMP', 'VIEW', datetime(2026, 11, 16, 7, 0, 0))
    objects = [emp, pkg, view]
    disk = {emp.relpath, pkg.relpath, 'scott/views/gone.sql'}
    to_write, to_delete = plan_actions(
        objects,
        disk,
        last_unload_at=stamp,
        lookback_minutes=0,
    )
    written = {obj.relpath for obj in to_write}
    assert pkg.relpath in written
    assert view.relpath in written
    assert emp.relpath not in written
    assert to_delete == {'scott/views/gone.sql'}


def test_plan_equal_last_ddl_is_rewritten():
    stamp = datetime(2026, 11, 16, 8, 0, 0, 123456)
    emp = _obj('EMP', 'TABLE', datetime(2026, 11, 16, 8, 0, 0, 654321))
    to_write, _to_delete = plan_actions(
        [emp],
        {emp.relpath},
        last_unload_at=stamp,
        lookback_minutes=0,
    )
    assert to_write == [emp]


def test_plan_lookback_includes_recent_objects():
    last = datetime(2026, 11, 16, 8, 0, 0)
    emp = _obj('EMP', 'TABLE', datetime(2026, 11, 16, 7, 56, 0))
    to_write, _to_delete = plan_actions(
        [emp],
        {emp.relpath},
        last_unload_at=last,
        lookback_minutes=5,
    )
    assert to_write == [emp]


def test_max_last_ddl_time_and_next_watermark():
    older = _obj('EMP', 'TABLE', datetime(2026, 11, 16, 7, 0, 0, 1))
    newer = _obj('DEPT', 'TABLE', datetime(2026, 11, 16, 8, 0, 0, 2))
    assert max_last_ddl_time([older, newer]) == datetime(2026, 11, 16, 8, 0, 0)
    assert next_watermark(newer.last_ddl_time, [older]) == datetime(2026, 11, 16, 8, 0, 0)


def test_normalize_object_types_aliases_and_expand():
    assert normalize_object_types(None) == list(ALL_OBJECT_TYPES)
    assert normalize_object_types(['package']) == ['PACKAGE', 'PACKAGE BODY']
    assert normalize_object_types(['mview']) == ['MATERIALIZED VIEW']


def test_normalize_object_types_unknown():
    with pytest.raises(ValueError, match='unknown object type'):
        normalize_object_types(['widget'])


def test_walk_schema_files_filters_by_type_suffix(tmp_path):
    pkg = tmp_path / 'scott' / 'packages'
    tables = tmp_path / 'scott' / 'tables'
    pkg.mkdir(parents=True)
    tables.mkdir(parents=True)
    (pkg / 'pkg_x_s.sql').write_text('-- spec\n', encoding='utf-8')
    (pkg / 'pkg_x_b.sql').write_text('-- body\n', encoding='utf-8')
    (tables / 'emp.sql').write_text('-- table\n', encoding='utf-8')

    assert walk_schema_files(tmp_path, 'SCOTT', ['PACKAGE']) == {
        'scott/packages/pkg_x_s.sql'
    }
    assert walk_schema_files(tmp_path, 'SCOTT', ['PACKAGE BODY']) == {
        'scott/packages/pkg_x_b.sql'
    }


def test_metadata_object_type_maps_bodies():
    assert metadata_object_type('FUNCTION') == 'FUNCTION'
    assert metadata_object_type('PACKAGE BODY') == 'PACKAGE_BODY'
    assert metadata_object_type('TYPE BODY') == 'TYPE_BODY'


def test_skip_objects_match_name_or_type():
    skip = normalize_skip_objects(
        ['scott.t1', 'HR.package body.PKG_X', '  ']
    )
    assert object_is_skipped(_obj('T1', 'TABLE', datetime(2026, 1, 1)), skip)
    assert not object_is_skipped(_obj('T2', 'TABLE', datetime(2026, 1, 1)), skip)
    assert object_is_skipped(
        _obj('PKG_X', 'PACKAGE BODY', datetime(2026, 1, 1), schema='HR'),
        skip,
    )
    assert not object_is_skipped(
        _obj('PKG_X', 'PACKAGE', datetime(2026, 1, 1), schema='HR'),
        skip,
    )
