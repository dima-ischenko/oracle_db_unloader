from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import text

from oracle_db_unloader import Unloader
from oracle_db_unloader.inventory import (
    fetch_inventory,
    max_last_ddl_time,
    normalize_ddl_time,
)
from oracle_db_unloader.state import get_last_unload_at, load_state


def _unload(
    engine,
    tmp_path,
    full=False,
    lookback_minutes=0,
    object_types=None,
    dependencies=False,
):
    client = Unloader(
        engine,
        schemas=['TEST'],
        output_dir=tmp_path,
        max_workers=2,
        object_types=object_types,
    )
    client.ddl(full=full, lookback_minutes=lookback_minutes)
    if dependencies:
        client.dependencies()
    client.custom_sql()


def test_unload_writes_expected_files_and_headers(fresh_schema, tmp_path):
    time.sleep(1.1)
    meta = tmp_path / '_meta'
    meta.mkdir()
    (meta / 'currency.sql').write_text(
        'select :schema as owner, code, name from currency_dict order by code',
        encoding='utf-8',
    )
    _unload(fresh_schema, tmp_path)

    table = Path(tmp_path) / 'test' / 'tables' / 'emp_heap.sql'
    emp_hash = Path(tmp_path) / 'test' / 'tables' / 'emp_hash.sql'
    sales = Path(tmp_path) / 'test' / 'tables' / 'sales_part.sql'
    camel = Path(tmp_path) / 'test' / 'tables' / 'mixedcase_metrics.sql'
    view = Path(tmp_path) / 'test' / 'views' / 'emp_v.sql'
    pkg_s = Path(tmp_path) / 'test' / 'packages' / 'pkg_demo_s.sql'
    dml = Path(tmp_path) / 'test' / '_meta' / 'currency.md'

    for path in (table, emp_hash, sales, camel, view, pkg_s, dml):
        assert path.is_file(), path

    table_sql = table.read_text(encoding='utf-8')
    assert 'create table test.' not in table_sql.lower()
    assert 'create table emp_heap' in table_sql.lower()
    assert '"EMP_HEAP"' not in table_sql
    assert '"ID"' not in table_sql

    sales_sql = sales.read_text(encoding='utf-8')
    assert 'ALTER TABLE sales_part MODIFY PARTITION BY RANGE (sale_date)' in sales_sql
    assert 'partition p2024' not in sales_sql.lower()

    hash_sql = emp_hash.read_text(encoding='utf-8')
    assert 'ALTER TABLE emp_hash MODIFY PARTITION BY HASH (id) PARTITIONS 4' in hash_sql

    camel_sql = camel.read_text(encoding='utf-8')
    assert '"MixedCase_metrics"' in camel_sql
    assert '"createdAt"' in camel_sql
    assert '"ID"' not in camel_sql
    assert 'comment on column "MixedCase_metrics"."createdAt"' in camel_sql

    dml_text = dml.read_text(encoding='utf-8')
    assert 'USD' in dml_text
    assert 'null' in dml_text

    table_stamp = get_last_unload_at(load_state(tmp_path, 'TEST'), 'TABLE')
    tables = [
        obj
        for obj in fetch_inventory(fresh_schema, 'TEST')
        if obj.object_type == 'TABLE'
    ]
    assert normalize_ddl_time(table_stamp) == max_last_ddl_time(tables)


def test_incremental_refresh_and_delete(fresh_schema, tmp_path):
    time.sleep(1.1)
    _unload(fresh_schema, tmp_path)

    pkg = Path(tmp_path) / 'test' / 'packages' / 'pkg_demo_s.sql'
    emp = Path(tmp_path) / 'test' / 'tables' / 'emp_heap.sql'
    view = Path(tmp_path) / 'test' / 'views' / 'emp_v.sql'
    emp_before = emp.read_text(encoding='utf-8')

    with fresh_schema.begin() as conn:
        conn.execute(
            text(
                """
                create or replace package pkg_demo as
                  function ping return number;
                  function pong return number;
                end pkg_demo;
                """
            )
        )
        conn.execute(text('drop view emp_v'))

    time.sleep(1.1)
    _unload(fresh_schema, tmp_path)

    assert 'pong' in pkg.read_text(encoding='utf-8').lower()
    assert emp.read_text(encoding='utf-8') == emp_before
    assert not view.exists()


def test_object_type_filter_keeps_other_types(fresh_schema, tmp_path):
    time.sleep(1.1)
    _unload(fresh_schema, tmp_path)
    view = Path(tmp_path) / 'test' / 'views' / 'emp_v.sql'
    assert view.is_file()

    with fresh_schema.begin() as conn:
        conn.execute(text('drop view emp_v'))

    time.sleep(1.1)
    _unload(fresh_schema, tmp_path, object_types=['TABLE'])
    assert view.is_file()

    _unload(fresh_schema, tmp_path, object_types=['VIEW'])
    assert not view.exists()


def test_dependencies_write_view_to_table_paths(fresh_schema, tmp_path):
    time.sleep(1.1)
    _unload(fresh_schema, tmp_path, object_types=['TABLE', 'VIEW'], dependencies=True)

    view_deps = Path(tmp_path) / 'test' / '_deps' / 'views' / 'emp_v.md'
    table_deps = Path(tmp_path) / 'test' / '_deps' / 'tables' / 'emp_heap.md'
    assert view_deps.is_file()
    assert table_deps.is_file()
    view_text = view_deps.read_text(encoding='utf-8')
    table_text = table_deps.read_text(encoding='utf-8')
    assert '## Downstream' in view_text
    assert '> test.emp_heap(table)' in view_text
    assert 'emp_v(view)' not in view_text
    assert '## Upstream writes' in table_text
    assert 'test.p_emp_heap_load(procedure) >' in table_text
    assert '## Upstream reads' in table_text
    assert '## Upstream\n' not in table_text
    assert 'test.emp_v(view) >' in table_text
    assert 'test.p_emp_heap_read(procedure) >' in table_text
    writes, sep, rest = table_text.partition('## Upstream reads\n')
    assert sep
    assert 'p_emp_heap_load' in writes
    assert 'p_emp_heap_load' not in rest
    assert 'emp_heap(table)' not in table_text
    assert not (
        Path(tmp_path) / 'test' / '_deps' / 'tables' / 'currency_dict.md'
    ).exists()


def test_index_change_rewrites_table(fresh_schema, tmp_path):
    time.sleep(1.1)
    _unload(fresh_schema, tmp_path, object_types=['TABLE'])
    emp = Path(tmp_path) / 'test' / 'tables' / 'emp_heap.sql'
    before = emp.read_text(encoding='utf-8')
    assert 'emp_heap_name_i' not in before.lower()

    with fresh_schema.begin() as conn:
        conn.execute(text('create index emp_heap_name_i on emp_heap (name)'))

    time.sleep(1.1)
    _unload(fresh_schema, tmp_path, object_types=['TABLE'])
    after = emp.read_text(encoding='utf-8')
    assert 'emp_heap_name_i' in after.lower()
