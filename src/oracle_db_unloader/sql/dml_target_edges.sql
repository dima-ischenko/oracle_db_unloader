with stmts as (
  select owner,
         object_name,
         object_type,
         type,
         usage_id,
         line,
         col,
         lead(line) over (
           partition by owner, object_name, object_type
           order by line, col
         ) as next_line,
         lead(col) over (
           partition by owner, object_name, object_type
           order by line, col
         ) as next_col
    from dba_statements
   where owner not in ('SYS', 'SYSTEM', 'PUBLIC')
),
dml as (
  select *
    from stmts
   where type in ('INSERT', 'UPDATE', 'DELETE', 'MERGE')
),
target as (
  select d.owner,
         d.object_name,
         d.object_type,
         di.name,
         di.type as ident_type,
         di.declared_owner,
         row_number() over (
           partition by d.owner, d.object_name, d.object_type, d.usage_id
           order by di.line, di.col
         ) as rn
    from dml d
    join dba_identifiers di
      on di.owner = d.owner
     and di.object_name = d.object_name
     and di.object_type = d.object_type
     and di.type in ('TABLE', 'VIEW', 'SYNONYM')
     and di.usage = 'REFERENCE'
     and (di.line > d.line or (di.line = d.line and di.col >= d.col))
     and (d.next_line is null
          or di.line < d.next_line
          or (di.line = d.next_line and di.col < d.next_col))
)
select distinct
       nvl(resolved.table_owner, target.declared_owner) as table_owner,
       nvl(resolved.table_name, target.name) as table_name,
       target.owner,
       target.object_name,
       target.object_type
  from target
  left join dba_synonyms resolved
    on target.ident_type = 'SYNONYM'
   and resolved.owner = target.declared_owner
   and resolved.synonym_name = target.name
 where target.rn = 1
   and target.declared_owner not in ('SYS', 'SYSTEM', 'PUBLIC')
   and nvl(resolved.table_owner, target.declared_owner) not in (
         'SYS',
         'SYSTEM',
         'PUBLIC'
       )
   and (
         target.ident_type = 'TABLE'
         or (
              target.ident_type = 'SYNONYM'
              and exists (
                    select 1
                      from dba_tables dt
                     where dt.owner = resolved.table_owner
                       and dt.table_name = resolved.table_name
                  )
            )
       )
