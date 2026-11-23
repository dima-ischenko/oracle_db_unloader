from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .resources import bind_params, load_sql

TYPE_FOLDERS = {
    'TABLE': 'tables',
    'VIEW': 'views',
    'MATERIALIZED VIEW': 'mviews',
    'PACKAGE': 'packages',
    'PACKAGE BODY': 'packages',
    'FUNCTION': 'functions',
    'PROCEDURE': 'procedures',
    'TRIGGER': 'triggers',
    'TYPE': 'types',
    'TYPE BODY': 'types',
    'SEQUENCE': 'sequences',
    'SYNONYM': 'synonyms',
}

TYPE_SUFFIX = {
    'PACKAGE': '_s',
    'PACKAGE BODY': '_b',
    'TYPE': '_s',
    'TYPE BODY': '_b',
}

UNLOAD_SQL = {
    'TABLE': 'unload_table.sql',
    'VIEW': 'unload_view.sql',
    'MATERIALIZED VIEW': 'unload_mview.sql',
    'PACKAGE': 'unload_source.sql',
    'PACKAGE BODY': 'unload_source.sql',
    'FUNCTION': 'unload_source.sql',
    'PROCEDURE': 'unload_source.sql',
    'TRIGGER': 'unload_source.sql',
    'TYPE': 'unload_source.sql',
    'TYPE BODY': 'unload_source.sql',
    'SEQUENCE': 'unload_sequence.sql',
    'SYNONYM': 'unload_synonym.sql',
}

SOURCE_METADATA_SQL = 'unload_source_ddl.sql'

SOURCE_TYPES = {
    'PACKAGE',
    'PACKAGE BODY',
    'FUNCTION',
    'PROCEDURE',
    'TRIGGER',
    'TYPE',
    'TYPE BODY',
}

# dbms_metadata.get_ddl uses underscores for body types.
METADATA_OBJECT_TYPES = {
    'PACKAGE BODY': 'PACKAGE_BODY',
    'TYPE BODY': 'TYPE_BODY',
}

HEADER_TYPES = {'TABLE', 'MATERIALIZED VIEW'}

ALL_OBJECT_TYPES = tuple(TYPE_FOLDERS)

_TYPE_ALIASES = {
    'table': 'TABLE',
    'tables': 'TABLE',
    'view': 'VIEW',
    'views': 'VIEW',
    'mview': 'MATERIALIZED VIEW',
    'mviews': 'MATERIALIZED VIEW',
    'materialized view': 'MATERIALIZED VIEW',
    'package': 'PACKAGE',
    'packages': 'PACKAGE',
    'package body': 'PACKAGE BODY',
    'function': 'FUNCTION',
    'functions': 'FUNCTION',
    'procedure': 'PROCEDURE',
    'procedures': 'PROCEDURE',
    'trigger': 'TRIGGER',
    'triggers': 'TRIGGER',
    'type': 'TYPE',
    'types': 'TYPE',
    'type body': 'TYPE BODY',
    'sequence': 'SEQUENCE',
    'sequences': 'SEQUENCE',
    'synonym': 'SYNONYM',
    'synonyms': 'SYNONYM',
}
for _name in ALL_OBJECT_TYPES:
    _TYPE_ALIASES[_name.lower()] = _name

_TYPE_EXPAND = {
    'PACKAGE': ('PACKAGE', 'PACKAGE BODY'),
    'TYPE': ('TYPE', 'TYPE BODY'),
}


def normalize_object_types(values: Optional[Sequence[str]] = None) -> List[str]:
    """Map user-facing type names to canonical Oracle object types.

    ``package`` / ``type`` expand to spec + body. Unknown names raise
    ``ValueError``. ``None`` or an empty list means every supported type.
    """
    if not values:
        return list(ALL_OBJECT_TYPES)
    selected: List[str] = []
    for raw in values:
        token = ' '.join(raw.strip().lower().replace('_', ' ').split())
        if not token:
            continue
        canonical = _TYPE_ALIASES.get(token)
        if canonical is None:
            raise ValueError(f'unknown object type: {raw}')
        for item in _TYPE_EXPAND.get(canonical, (canonical,)):
            if item not in selected:
                selected.append(item)
    if not selected:
        return list(ALL_OBJECT_TYPES)
    return selected


def metadata_object_type(object_type: str) -> str:
    """Oracle dictionary type -> dbms_metadata.get_ddl object_type."""
    return METADATA_OBJECT_TYPES.get(object_type, object_type)


def normalize_skip_objects(values: Optional[Sequence[str]] = None) -> FrozenSet[str]:
    """Parse ``SCHEMA.NAME`` or ``SCHEMA.TYPE.NAME`` skip keys.

    Type tokens accept spaces or underscores (``package body`` /
    ``PACKAGE_BODY``). Matching is case-insensitive.
    """
    skip: Set[str] = set()
    for raw in values or ():
        token = ' '.join(raw.strip().split())
        if not token:
            continue
        skip.add(token.upper().replace(' ', '_'))
    return frozenset(skip)


def object_is_skipped(obj: DbObject, skip_objects: FrozenSet[str]) -> bool:
    if not skip_objects:
        return False
    owner = obj.owner.upper()
    name = obj.name.upper()
    typ = obj.object_type.upper().replace(' ', '_')
    return (
        f'{owner}.{name}' in skip_objects
        or f'{owner}.{typ}.{name}' in skip_objects
    )


@dataclass(frozen=True)
class DbObject:
    schema: str
    owner: str
    name: str
    object_type: str
    last_ddl_time: Optional[datetime]

    @property
    def relpath(self) -> str:
        return object_relpath(self.schema, self.name, self.object_type)

    @property
    def deps_relpath(self) -> str:
        return object_deps_relpath(self.schema, self.name, self.object_type)


def object_relpath(schema: str, name: str, object_type: str) -> str:
    folder = TYPE_FOLDERS[object_type]
    suffix = TYPE_SUFFIX.get(object_type, '')
    filename = f'{name.lower()}{suffix}.sql'
    return f'{schema.lower()}/{folder}/{filename}'


def object_deps_relpath(schema: str, name: str, object_type: str) -> str:
    folder = TYPE_FOLDERS[object_type]
    suffix = TYPE_SUFFIX.get(object_type, '')
    filename = f'{name.lower()}{suffix}.md'
    return f'{schema.lower()}/_deps/{folder}/{filename}'


def normalize_ddl_time(value: Optional[datetime]) -> Optional[datetime]:
    """Keep Oracle last_ddl_time as a naive timestamp truncated to seconds.

    Do not assume UTC: the watermark must live in the same clock as
    ``dba_objects.last_ddl_time``.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.replace(microsecond=0)


def max_last_ddl_time(objects: Iterable[DbObject]) -> Optional[datetime]:
    times = [
        normalize_ddl_time(obj.last_ddl_time)
        for obj in objects
        if obj.last_ddl_time is not None
    ]
    times = [item for item in times if item is not None]
    if not times:
        return None
    return max(times)


def next_watermark(
    previous: Optional[datetime],
    objects: Iterable[DbObject],
) -> Optional[datetime]:
    current = max_last_ddl_time(objects)
    prev = normalize_ddl_time(previous)
    if current is None:
        return prev
    if prev is None:
        return current
    return max(prev, current)


def plan_actions(
    objects: Iterable[DbObject],
    disk_relpaths: Set[str],
    last_unload_at: Optional[datetime],
    lookback_minutes: int,
) -> Tuple[List[DbObject], Set[str]]:
    object_list = list(objects)
    cutoff = None
    if last_unload_at is not None:
        cutoff = normalize_ddl_time(last_unload_at)
        if cutoff is not None and lookback_minutes:
            cutoff = cutoff - timedelta(minutes=lookback_minutes)

    to_write: List[DbObject] = []
    known_paths = {obj.relpath for obj in object_list}
    for obj in object_list:
        missing = obj.relpath not in disk_relpaths
        if cutoff is None or missing:
            to_write.append(obj)
            continue
        ddl_time = normalize_ddl_time(obj.last_ddl_time)
        if ddl_time is None or ddl_time >= cutoff:
            to_write.append(obj)

    to_delete = {path for path in disk_relpaths if path not in known_paths}
    return to_write, to_delete


def walk_schema_files(
    output_dir: Path,
    schema: str,
    object_types: Optional[Sequence[str]] = None,
) -> Set[str]:
    types = list(object_types) if object_types else list(ALL_OBJECT_TYPES)
    found: Set[str] = set()
    for object_type in types:
        found.update(_walk_type_files(output_dir, schema, object_type))
    return found


def _walk_type_files(output_dir: Path, schema: str, object_type: str) -> Set[str]:
    folder = TYPE_FOLDERS[object_type]
    directory = Path(output_dir) / schema.lower() / folder
    if not directory.is_dir():
        return set()
    suffix = TYPE_SUFFIX.get(object_type)
    sibling_suffixes = [
        TYPE_SUFFIX[other]
        for other, other_folder in TYPE_FOLDERS.items()
        if other_folder == folder and other in TYPE_SUFFIX and other != object_type
    ]
    found: Set[str] = set()
    for path in directory.glob('*.sql'):
        name = path.name.lower()
        if suffix:
            if not name.endswith(f'{suffix}.sql'):
                continue
        elif any(name.endswith(f'{item}.sql') for item in sibling_suffixes):
            continue
        found.add(path.relative_to(output_dir).as_posix())
    return found


def fetch_inventory(engine: Engine, schema: str) -> List[DbObject]:
    sql = load_sql('inventory.sql')
    schema_u = schema.upper()
    with engine.connect() as conn:
        rows = conn.execute(text(sql), bind_params(sql, {'schema': schema_u}))
        by_path = {}
        for owner, name, object_type, last_ddl_time in rows:
            obj = DbObject(
                schema=schema_u,
                owner=owner,
                name=name,
                object_type=object_type,
                last_ddl_time=last_ddl_time,
            )
            existing = by_path.get(obj.relpath)
            if existing is None or (
                existing.owner == 'PUBLIC' and obj.owner != 'PUBLIC'
            ):
                by_path[obj.relpath] = obj
        return list(by_path.values())
