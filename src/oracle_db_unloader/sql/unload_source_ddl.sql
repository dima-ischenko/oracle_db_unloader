select dbms_metadata.get_ddl(:obj_type, :obj_name, upper(:schema)) as ddl
  from dual
