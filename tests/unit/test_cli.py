from __future__ import annotations

import pytest

from oracle_db_unloader.cli import _build_url, parse_args


def test_parse_db_url_and_defaults():
    args = parse_args(
        [
            '--db-url',
            'oracle+oracledb://u:p@localhost:1521/?service_name=FREEPDB1',
            '--schemas',
            'scott,hr',
            '--output-dir',
            '/tmp/ddl',
        ]
    )
    assert args.schemas == 'scott,hr'
    assert args.lookback_minutes == 0
    assert args.object_types is None
    assert args.dependencies is False


def test_parse_login_and_flags():
    args = parse_args(
        [
            '--db-login',
            'system',
            '--db-pass',
            'secret',
            '--db-tns',
            'orclpdb',
            '--schemas',
            'scott',
            '--output-dir',
            'out',
            '--full',
            '--object-types',
            'table,package',
            '--dependencies',
        ]
    )
    assert args.full is True
    assert args.object_types == ['TABLE', 'PACKAGE', 'PACKAGE BODY']
    assert args.dependencies is True
    assert args.continue_on_error is False
    assert args.skip_objects is None


def test_parse_skip_and_continue_on_error():
    args = parse_args(
        [
            '--db-url',
            'oracle+oracledb://u:p@x',
            '--schemas',
            'scott',
            '--output-dir',
            'out',
            '--skip-objects',
            'ML_CALL_ROUTING.F_CALL_ROUTING_PREPARE,scott.package body.pkg_x',
            '--continue-on-error',
        ]
    )
    assert args.continue_on_error is True
    assert args.skip_objects == [
        'ML_CALL_ROUTING.F_CALL_ROUTING_PREPARE',
        'scott.package body.pkg_x',
    ]


def test_unknown_object_type_errors():
    with pytest.raises(SystemExit):
        parse_args(
            [
                '--db-url',
                'oracle+oracledb://u:p@x',
                '--schemas',
                'scott',
                '--output-dir',
                'out',
                '--object-types',
                'widget',
            ]
        )


def test_missing_connection_errors():
    with pytest.raises(SystemExit):
        parse_args(['--schemas', 'scott', '--output-dir', 'out'])


def test_build_url_encodes_password():
    url = _build_url(db_login='system', db_pass='p@ss', db_tns='orclpdb')
    assert url == 'oracle+oracledb://system:p%40ss@orclpdb'
