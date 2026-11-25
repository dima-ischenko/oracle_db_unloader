select owner,
       object_name,
       object_type,
       last_ddl_time
  from (
        -- Tables / mviews: last_ddl_time is greatest(object, partitions, indexes)
        select t.owner,
               t.object_name,
               t.object_type,
               max(d.last_ddl_time) as last_ddl_time
          from dba_objects t
          join (
                select o.owner,
                       o.object_name as table_name,
                       o.last_ddl_time
                  from dba_objects o
                 where o.owner = upper(:schema)
                   and o.object_type in (
                         'TABLE',
                         'MATERIALIZED VIEW',
                         'TABLE PARTITION',
                         'TABLE SUBPARTITION'
                       )
                union all
                select i.owner,
                       i.table_name,
                       o.last_ddl_time
                  from dba_indexes i
                  join dba_objects o
                    on o.owner = i.owner
                   and o.object_name = i.index_name
                   and o.object_type in (
                         'INDEX',
                         'INDEX PARTITION',
                         'INDEX SUBPARTITION'
                       )
                 where i.owner = upper(:schema)
               ) d
            on d.owner = t.owner
           and d.table_name = t.object_name
         where t.owner = upper(:schema)
           and t.object_type in ('TABLE', 'MATERIALIZED VIEW')
           and t.object_name not like 'RB$%'
           and t.object_name not like '%#%'
           and not (
                 t.object_type = 'TABLE'
                 and exists (
                       select null
                         from dba_mviews m
                        where m.owner = t.owner
                          and m.mview_name = t.object_name
                     )
               )
         group by t.owner, t.object_name, t.object_type
        union all
        select o.owner,
               o.object_name,
               o.object_type,
               o.last_ddl_time
          from dba_objects o
         where o.owner = upper(:schema)
           and o.object_type in (
                 'VIEW',
                 'PACKAGE',
                 'PACKAGE BODY',
                 'FUNCTION',
                 'PROCEDURE',
                 'TRIGGER',
                 'TYPE',
                 'TYPE BODY',
                 'SEQUENCE',
                 'SYNONYM'
               )
           and o.object_name not like 'RB$%'
        union all
        select s.owner,
               s.synonym_name as object_name,
               'SYNONYM' as object_type,
               o.last_ddl_time
          from dba_synonyms s
          join dba_objects o
            on o.owner = s.owner
           and o.object_name = s.synonym_name
           and o.object_type = 'SYNONYM'
         where s.owner = 'PUBLIC'
           and s.table_owner = upper(:schema)
       ) objects
    where not exists (select null 
                from dba_nested_tables dn
              where dn.owner = objects.owner
                and dn.table_name = objects.object_name 
                and objects.object_type = 'TABLE'
              )   
 order by object_type, object_name
