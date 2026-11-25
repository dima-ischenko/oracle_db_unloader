from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

RESET_SQL = """
begin
  for rec in (
    select object_type, object_name
      from user_objects
     where object_name in (
             'EMP_HEAP',
             'EMP_HASH',
             'SALES_PART',
             'EMP_V',
             'PKG_DEMO',
             'SEQ_DEMO',
             'SYN_EMP',
             'CURRENCY_DICT',
             'MixedCase_metrics',
             'EMP_HEAP_NAME_I',
             'P_EMP_HEAP_LOAD',
             'P_EMP_HEAP_READ'
           )
  ) loop
    begin
      if rec.object_type = 'TABLE' then
        execute immediate 'drop table "' || rec.object_name || '" cascade constraints purge';
      elsif rec.object_type = 'PACKAGE BODY' then
        execute immediate 'drop package body "' || rec.object_name || '"';
      else
        execute immediate 'drop ' || rec.object_type || ' "' || rec.object_name || '"';
      end if;
    exception
      when others then
        null;
    end;
  end loop;
end;
"""

CREATE_STATEMENTS = [
    "alter session set plscope_settings = 'identifiers:all, statements:all'",
    """
    create table emp_heap (
      id number primary key,
      name varchar2(32)
    )
    """,
    "comment on table emp_heap is 'heap employees'",
    """
    create table emp_hash (
      id number,
      name varchar2(32)
    )
    partition by hash (id) partitions 4
    """,
    "comment on column emp_heap.name is 'employee name'",
    "insert into emp_heap (id, name) values (1, 'alice')",
    "insert into emp_heap (id, name) values (2, 'bob')",
    "insert into emp_heap (id, name) values (3, 'carol')",
    """
    create table sales_part (
      id number,
      source_id number,
      sale_date date,
      amount number
    )
    partition by range (sale_date) interval (numtoyminterval(1, 'month'))
    subpartition by list (source_id)
    subpartition template (
      subpartition s1 values (1),
      subpartition s2 values (2),
      subpartition s3 values (3)
    ) (
      partition p0 values less than (date '2020-01-01')
    )
    """,
    "insert into sales_part (id, source_id, sale_date, amount) values (1, 1, date '2024-01-15', 10)",
    "insert into sales_part (id, source_id, sale_date, amount) values (2, 2, date '2024-02-15', 20)",
    "insert into sales_part (id, source_id, sale_date, amount) values (3, 3, date '2024-03-15', 30)",
    "create or replace view emp_v as select id, name from emp_heap",
    """
    create or replace package pkg_demo as
      function ping return number;
    end pkg_demo;
    """,
    """
    create or replace package body pkg_demo as
      function ping return number is
      begin
        return 1;
      end;
    end pkg_demo;
    """,
    'create sequence seq_demo',
    'create or replace synonym syn_emp for emp_heap',
    """
    create table "MixedCase_metrics" (
      id number,
      "createdAt" varchar2(32)
    )
    """,
    'comment on table "MixedCase_metrics" is \'camel case metrics\'',
    'comment on column "MixedCase_metrics"."createdAt" is \'camel case column\'',
    """
    create table currency_dict (
      code varchar2(3),
      name varchar2(32)
    )
    """,
    "insert into currency_dict (code, name) values ('USD', 'US Dollar')",
    "insert into currency_dict (code, name) values ('EUR', null)",
    """
    create or replace procedure p_emp_heap_load as
    begin
      insert into emp_heap (id, name) values (4, 'dave');
    end;
    """,
    """
    create or replace procedure p_emp_heap_read as
      n number;
    begin
      select count(*) into n from emp_heap;
    end;
    """,
]


def reset_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(RESET_SQL))
        for statement in CREATE_STATEMENTS:
            conn.execute(text(statement))
