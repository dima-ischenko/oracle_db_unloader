select xmlcast(
         xmlagg(
           xmlelement(
             e,
             xmlcdata(
               decode(
                 d.line,
                 1,
                 'create or replace ' || replace(d.text, '"' || d.owner || '".'),
                 nvl(d.text, '')
               )
             )
           )
           order by d.line
         ) as clob
       ) as ddl
  from dba_source d
 where d.type = :obj_type
   and d.owner = upper(:schema)
   and d.name = :obj_name
