from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from oracle_db_unloader.dependencies import (
    Node,
    build_graph,
    dependency_rows,
    dump_dependencies,
    format_dep_markdown,
    write_schema_dependencies,
)
from oracle_db_unloader.inventory import DbObject


def _edge(src, dst):
    s_owner, s_name, s_type = src
    d_owner, d_name, d_type = dst
    return Node(s_owner, s_name, s_type), Node(d_owner, d_name, d_type)


def _obj(schema, name, object_type, owner=None):
    return DbObject(
        schema=schema,
        owner=owner or schema,
        name=name,
        object_type=object_type,
        last_ddl_time=datetime(2026, 11, 16, 8, 0, 0),
    )


def _rows_by_type(rows):
    return {
        'upstream': [row for row in rows if row.ref_type == 'upstream'],
        'downstream': [row for row in rows if row.ref_type == 'downstream'],
    }


def test_direct_downstream_omits_root():
    graph = build_graph(
        [
            _edge(('HR', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP', 'TABLE')),
        ]
    )
    rows = _rows_by_type(dependency_rows(graph, Node('HR', 'EMP_V', 'VIEW')))
    assert [row.xref_path for row in rows['downstream']] == ['> scott.emp(table)']
    assert rows['upstream'] == []


def test_direct_upstream_omits_root():
    graph = build_graph(
        [
            _edge(('HR', 'P_REPORT', 'PROCEDURE'), ('SCOTT', 'EMP_V', 'VIEW')),
        ]
    )
    rows = _rows_by_type(dependency_rows(graph, Node('SCOTT', 'EMP_V', 'VIEW')))
    assert [row.xref_path for row in rows['upstream']] == ['hr.p_report(procedure) >']
    assert rows['downstream'] == []


def test_multihop_and_diamond_paths():
    graph = build_graph(
        [
            _edge(('HR', 'PKG_REPORT', 'PACKAGE BODY'), ('SCOTT', 'EMP_V', 'VIEW')),
            _edge(('SCOTT', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP_V2', 'VIEW')),
            _edge(('SCOTT', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP', 'TABLE')),
            _edge(('SCOTT', 'EMP_V2', 'VIEW'), ('SCOTT', 'EMP', 'TABLE')),
            _edge(('HR', 'DEPT_V', 'VIEW'), ('SCOTT', 'EMP_V', 'VIEW')),
            _edge(('HR', 'DEPT_V2', 'VIEW'), ('HR', 'DEPT_V', 'VIEW')),
            _edge(('HR', 'OTHER_V', 'VIEW'), ('SCOTT', 'EMP_V', 'VIEW')),
        ]
    )
    down = _rows_by_type(
        dependency_rows(graph, Node('HR', 'PKG_REPORT', 'PACKAGE BODY'))
    )['downstream']
    assert [row.xref_path for row in down] == [
        '> scott.emp_v(view) > scott.emp(table)',
        '> scott.emp_v(view) > scott.emp_v2(view) > scott.emp(table)',
    ]

    up = _rows_by_type(dependency_rows(graph, Node('SCOTT', 'EMP_V', 'VIEW')))[
        'upstream'
    ]
    assert [row.xref_path for row in up] == [
        'hr.dept_v2(view) > hr.dept_v(view) >',
        'hr.other_v(view) >',
        'hr.pkg_report(package body) >',
    ]


def test_cycle_does_not_loop():
    graph = build_graph(
        [
            _edge(('A', 'T1', 'TABLE'), ('A', 'T2', 'TABLE')),
            _edge(('A', 'T2', 'TABLE'), ('A', 'T1', 'TABLE')),
        ]
    )
    rows = dependency_rows(graph, Node('A', 'T1', 'TABLE'))
    assert {row.xref_path for row in rows} == {
        'a.t2(table) >',
        '> a.t2(table)',
    }


def test_write_schema_dependencies_layout_and_stale_delete(tmp_path):
    graph = build_graph(
        [
            _edge(('SCOTT', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP', 'TABLE')),
            _edge(('SCOTT', 'PKG_X', 'PACKAGE BODY'), ('SCOTT', 'EMP_V', 'VIEW')),
        ]
    )
    stale = tmp_path / 'scott' / '_deps' / 'views' / 'gone.md'
    stale.parent.mkdir(parents=True)
    stale.write_text('stale\n', encoding='utf-8')
    other_type = tmp_path / 'scott' / '_deps' / 'tables' / 'emp.md'
    other_type.parent.mkdir(parents=True)
    other_type.write_text('keep-if-not-selected\n', encoding='utf-8')
    empty_prev = tmp_path / 'scott' / '_deps' / 'views' / 'lonely.md'
    empty_prev.write_text('was-deps\n', encoding='utf-8')

    written = write_schema_dependencies(
        tmp_path,
        'SCOTT',
        [
            _obj('SCOTT', 'EMP_V', 'VIEW'),
            _obj('SCOTT', 'LONELY', 'VIEW'),
            _obj('SCOTT', 'PKG_X', 'PACKAGE BODY'),
            _obj('SCOTT', 'EMP', 'TABLE'),
        ],
        ['VIEW'],
        graph,
    )
    assert written == 1
    path = tmp_path / 'scott' / '_deps' / 'views' / 'emp_v.md'
    text = path.read_text(encoding='utf-8')
    assert text == (
        '## Downstream\n'
        '\n'
        '> scott.emp(table)\n'
        '\n'
        '## Upstream\n'
        '\n'
        'scott.pkg_x(package body) >\n'
    )
    assert 'scott.emp_v(view)' not in text
    assert not stale.exists()
    assert not empty_prev.exists()
    assert other_type.read_text(encoding='utf-8') == 'keep-if-not-selected\n'
    assert not (tmp_path / 'scott' / '_deps' / 'packages' / 'pkg_x_b.md').exists()


def test_dump_dependencies_fetches_once_and_writes(monkeypatch, tmp_path):
    calls = {'edges': 0, 'inventory': 0}

    def fake_edges(_engine):
        calls['edges'] += 1
        return [
            _edge(('SCOTT', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP', 'TABLE')),
            _edge(('HR', 'EMP_V', 'VIEW'), ('HR', 'EMP', 'TABLE')),
        ]

    def fake_inventory(_engine, schema):
        calls['inventory'] += 1
        return [_obj(schema, 'EMP_V', 'VIEW'), _obj(schema, 'EMP', 'TABLE')]

    monkeypatch.setattr(
        'oracle_db_unloader.dependencies.fetch_dependency_edges', fake_edges
    )
    monkeypatch.setattr(
        'oracle_db_unloader.dependencies.fetch_inventory', fake_inventory
    )

    dumped = dump_dependencies(
        MagicMock(),
        tmp_path,
        ['scott', 'hr'],
        ['VIEW', 'TABLE'],
        max_workers=3,
    )
    assert dumped == 4
    assert calls == {'edges': 1, 'inventory': 2}
    assert (tmp_path / 'scott' / '_deps' / 'views' / 'emp_v.md').is_file()
    assert (tmp_path / 'hr' / '_deps' / 'tables' / 'emp.md').is_file()


def test_format_dep_markdown_omits_empty_section():
    graph = build_graph([_edge(('HR', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP', 'TABLE'))])
    rows = dependency_rows(graph, Node('HR', 'EMP_V', 'VIEW'))
    assert format_dep_markdown(rows) == ('## Downstream\n\n> scott.emp(table)\n')


def test_write_schema_dependencies_logs_workers(tmp_path, caplog):
    graph = build_graph(
        [
            _edge(('SCOTT', 'EMP_V', 'VIEW'), ('SCOTT', 'EMP', 'TABLE')),
            _edge(('SCOTT', 'DEPT_V', 'VIEW'), ('SCOTT', 'DEPT', 'TABLE')),
        ]
    )
    caplog.set_level(logging.INFO, logger='oracle_db_unloader')
    written = write_schema_dependencies(
        tmp_path,
        'SCOTT',
        [
            _obj('SCOTT', 'EMP_V', 'VIEW'),
            _obj('SCOTT', 'DEPT_V', 'VIEW'),
            _obj('SCOTT', 'EMP', 'TABLE'),
        ],
        ['VIEW'],
        graph,
        max_workers=5,
    )
    assert written == 2
    assert 'dependencies schema SCOTT: inventory=3 selected=2 workers=2' in caplog.text
    assert (tmp_path / 'scott' / '_deps' / 'views' / 'emp_v.md').is_file()
    assert (tmp_path / 'scott' / '_deps' / 'views' / 'dept_v.md').is_file()


def test_write_schema_dependencies_failure_stops_remaining(monkeypatch, tmp_path):
    graph = build_graph(
        [
            _edge(('SCOTT', 'T1', 'TABLE'), ('SCOTT', 'T2', 'TABLE')),
            _edge(('SCOTT', 'T2', 'TABLE'), ('SCOTT', 'T3', 'TABLE')),
        ]
    )
    written = []

    def fake_write(output_dir, obj, _graph):
        written.append(obj.name)
        if obj.name == 'T1':
            raise RuntimeError('boom')
        return obj.deps_relpath, 1

    monkeypatch.setattr(
        'oracle_db_unloader.dependencies._write_object_deps', fake_write
    )
    with pytest.raises(RuntimeError, match='TABLE SCOTT.T1'):
        write_schema_dependencies(
            tmp_path,
            'SCOTT',
            [_obj('SCOTT', 'T1', 'TABLE'), _obj('SCOTT', 'T2', 'TABLE')],
            ['TABLE'],
            graph,
            max_workers=1,
        )
    assert written == ['T1']
