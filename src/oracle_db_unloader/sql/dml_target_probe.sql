select 1
  from dual
 where exists (
         select 1
           from dba_statements
          where type in ('INSERT', 'UPDATE', 'DELETE', 'MERGE')
            and owner not in ('SYS', 'SYSTEM', 'PUBLIC')
       )
