from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, NoReturn, Optional, Sequence, Set, Union

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .dependencies import dump_dependencies
from .dml import dump_custom_sql
from .inventory import (
    HEADER_TYPES,
    SOURCE_METADATA_SQL,
    SOURCE_TYPES,
    UNLOAD_SQL,
    DbObject,
    fetch_inventory,
    metadata_object_type,
    next_watermark,
    normalize_object_types,
    normalize_skip_objects,
    object_is_skipped,
    plan_actions,
    walk_schema_files,
)
from .logger import get_logger
from .metadata import (
    fetch_partition_info,
    format_partition_template_block,
    is_partition_shape_supported,
)
from .resources import (
    as_text,
    bind_params,
    concat_ddl,
    ensure_semicolon,
    load_sql,
    split_statements,
    strip_owner_qualifiers,
    unquote_simple_idents,
)
from .state import get_last_unload_at, load_state, save_state, set_last_unload_at

logger = get_logger(__name__)


def apply_session_transforms(conn: Connection) -> None:
    """Set dbms_metadata session transforms on an open connection."""
    conn.exec_driver_sql(load_sql('set_transform_params.sql'))


class UnloadError(RuntimeError):
    """Raised when unload cannot continue."""


@dataclass
class RunStats:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    failures: List[str] = field(default_factory=list)

    def add(
        self,
        added: int = 0,
        updated: int = 0,
        deleted: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> None:
        self.added += added
        self.updated += updated
        self.deleted += deleted
        self.skipped += skipped
        self.failed += failed


def _fetch_ddl_sql(conn: Connection, obj: DbObject, sql_name: str, obj_type: str) -> str:
    params = {
        'schema': obj.owner,
        'obj_name': obj.name,
        'obj_type': obj_type,
    }
    parts: List[str] = []
    for statement in split_statements(load_sql(sql_name)):
        result = conn.execute(text(statement), bind_params(statement, params))
        if not result.returns_rows:
            continue
        for row in result:
            part = as_text(row[0])
            if obj.object_type not in SOURCE_TYPES:
                part = ensure_semicolon(part)
            parts.append(part)
    ddl = unquote_simple_idents(strip_owner_qualifiers(concat_ddl(parts), obj.owner))
    if obj.object_type in SOURCE_TYPES:
        stripped = ddl.rstrip()
        if not stripped.endswith('/'):
            ddl = stripped + '\n/\n'
    return ddl


def fetch_object_ddl(conn: Connection, obj: DbObject) -> str:
    sql_name = UNLOAD_SQL[obj.object_type]
    try:
        return _fetch_ddl_sql(conn, obj, sql_name, obj.object_type)
    except Exception as source_exc:
        if obj.object_type not in SOURCE_TYPES:
            raise
        logger.warning(
            'dba_source failed for %s %s.%s (%s); falling back to dbms_metadata.get_ddl',
            obj.object_type,
            obj.owner,
            obj.name,
            source_exc,
        )
        try:
            return _fetch_ddl_sql(
                conn,
                obj,
                SOURCE_METADATA_SQL,
                metadata_object_type(obj.object_type),
            )
        except Exception as meta_exc:
            logger.exception(
                'dbms_metadata.get_ddl fallback failed for %s %s.%s',
                obj.object_type,
                obj.owner,
                obj.name,
            )
            raise source_exc from meta_exc


def write_object(engine: Engine, obj: DbObject, output_dir: Path) -> None:
    with engine.connect() as conn:
        apply_session_transforms(conn)
        ddl = fetch_object_ddl(conn, obj)
        footer = ''
        if obj.object_type in HEADER_TYPES:
            partition = fetch_partition_info(conn, obj)
            if partition is None:
                pass
            elif is_partition_shape_supported(partition):
                footer = format_partition_template_block(obj.name, partition)
            else:
                logger.warning(
                    'partition definition skipped, unsupported shape '
                    'table=%s.%s partitioning=%s subpartitioning=%s',
                    obj.owner,
                    obj.name,
                    partition.partitioning_type,
                    partition.subpartitioning_type or 'NONE',
                )
        content = ddl
        if footer:
            content = content.rstrip() + '\n\n' + footer + '\n'
        path = output_dir / obj.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')


def _delete_files(
    output_dir: Path,
    relpaths: Iterable[str],
    remaining_writes: int,
    schema: str,
    object_type: str,
) -> int:
    paths = sorted(relpaths)
    remaining_deletes = len(paths)
    deleted = 0
    for relpath in paths:
        path = output_dir / relpath
        remaining_deletes -= 1
        if path.is_file():
            path.unlink()
            deleted += 1
            logger.info(
                'deleted %s schema=%s type=%s remaining=%d delete=%d',
                path.as_posix(),
                schema,
                object_type,
                remaining_writes,
                remaining_deletes,
            )
    return deleted


def _normalize_schemas(schemas: Sequence[str]) -> List[str]:
    seen = []
    for schema in schemas:
        name = schema.strip().upper()
        if name and name not in seen:
            seen.append(name)
    if not seen:
        raise ValueError('at least one schema is required')
    return seen


def _write_objects(
    engine: Engine,
    to_write: Sequence[DbObject],
    disk: Set[str],
    output_dir: Path,
    remaining_deletes: int,
    max_workers: int,
    stats: RunStats,
    schema: str,
    object_type: str,
    continue_on_error: bool,
) -> None:
    remaining_writes = len(to_write)
    if not to_write:
        return
    workers = max(1, min(max_workers, remaining_writes))
    iterator = iter(to_write)
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    try:
        for _ in range(workers):
            obj = next(iterator, None)
            if obj is None:
                break
            futures[pool.submit(write_object, engine, obj, output_dir)] = obj
        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                obj = futures.pop(future)
                remaining_writes -= 1
                failed = False
                try:
                    future.result()
                except Exception as exc:
                    logger.exception(
                        'failed to unload %s %s.%s',
                        obj.object_type,
                        obj.owner,
                        obj.name,
                    )
                    if not continue_on_error:
                        raise UnloadError(
                            f'failed to unload {obj.object_type} {obj.owner}.{obj.name}'
                        ) from exc
                    failed = True
                    stats.add(failed=1)
                    stats.failures.append(f'{obj.object_type} {obj.owner}.{obj.name}')
                if not failed:
                    if obj.relpath in disk:
                        stats.add(updated=1)
                    else:
                        stats.add(added=1)
                    logger.info(
                        'unloaded %s schema=%s type=%s remaining=%d delete=%d',
                        (output_dir / obj.relpath).as_posix(),
                        schema,
                        object_type,
                        remaining_writes,
                        remaining_deletes,
                    )
                nxt = next(iterator, None)
                if nxt is not None:
                    futures[pool.submit(write_object, engine, nxt, output_dir)] = nxt
    finally:
        for future in list(futures):
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def _unload_object_type(
    engine: Engine,
    schema: str,
    object_type: str,
    objects: Sequence[DbObject],
    output_dir: Path,
    *,
    last_unload_at: Optional[datetime],
    lookback_minutes: int,
    max_workers: int,
    full: bool,
    stats: RunStats,
    skip_objects: FrozenSet[str],
    continue_on_error: bool,
) -> int:
    disk = walk_schema_files(output_dir, schema, [object_type])
    watermark = None if full else last_unload_at
    to_write, to_delete = plan_actions(objects, disk, watermark, lookback_minutes)
    skipped = [obj for obj in to_write if object_is_skipped(obj, skip_objects)]
    if skipped:
        skip_names = {obj.relpath for obj in skipped}
        to_write = [obj for obj in to_write if obj.relpath not in skip_names]
        stats.add(skipped=len(skipped))
        for obj in skipped:
            logger.warning(
                'skipped %s %s.%s',
                obj.object_type,
                obj.owner,
                obj.name,
            )
    workers = max(1, min(max_workers, len(to_write) or 1))
    logger.info(
        'schema %s type %s: inventory=%d write=%d delete=%d skip=%d workers=%d watermark=%s',
        schema,
        object_type,
        len(objects),
        len(to_write),
        len(to_delete),
        len(skipped),
        workers,
        watermark.isoformat() if watermark else 'none',
    )
    failed_before = stats.failed
    _write_objects(
        engine,
        to_write,
        disk,
        output_dir,
        remaining_deletes=len(to_delete),
        max_workers=max_workers,
        stats=stats,
        schema=schema,
        object_type=object_type,
        continue_on_error=continue_on_error,
    )
    deleted = _delete_files(
        output_dir,
        to_delete,
        remaining_writes=0,
        schema=schema,
        object_type=object_type,
    )
    stats.add(deleted=deleted)
    return stats.failed - failed_before


def unload_schema(
    engine: Engine,
    schema: str,
    output_dir: Path,
    *,
    object_types: Sequence[str],
    lookback_minutes: int,
    max_workers: int,
    full: bool,
    stats: RunStats,
    skip_objects: FrozenSet[str],
    continue_on_error: bool,
) -> None:
    inventory = fetch_inventory(engine, schema)
    by_type: Dict[str, List[DbObject]] = {
        object_type: [] for object_type in object_types
    }
    for obj in inventory:
        if obj.object_type in by_type:
            by_type[obj.object_type].append(obj)

    state = load_state(output_dir, schema)
    for object_type in object_types:
        type_failed = _unload_object_type(
            engine,
            schema,
            object_type,
            by_type[object_type],
            output_dir,
            last_unload_at=get_last_unload_at(state, object_type),
            lookback_minutes=lookback_minutes,
            max_workers=max_workers,
            full=full,
            stats=stats,
            skip_objects=skip_objects,
            continue_on_error=continue_on_error,
        )
        if type_failed:
            logger.warning(
                'schema %s type %s: watermark not moved, %d object(s) failed',
                schema,
                object_type,
                type_failed,
            )
            logger.info(
                'schema %s type %s: done watermark=%s',
                schema,
                object_type,
                'unchanged',
            )
            continue
        watermark = next_watermark(
            get_last_unload_at(state, object_type),
            by_type[object_type],
        )
        if watermark is not None:
            state = set_last_unload_at(state, watermark, object_type)
            save_state(output_dir, schema, state)
        logger.info(
            'schema %s type %s: done watermark=%s',
            schema,
            object_type,
            watermark.isoformat() if watermark else 'none',
        )


class Unloader:
    """Caller-owned engine plus output tree; each extractor is a separate method.

    The engine is never disposed here. Apply pool sizing on the engine you
    pass in; ``max_workers`` caps parallel DDL, ``_deps``, and custom-sql workers.
    """

    def __init__(
        self,
        engine: Engine,
        schemas: Sequence[str],
        output_dir: Union[str, Path],
        *,
        max_workers: int = 5,
        object_types: Optional[Sequence[str]] = None,
        skip_objects: Optional[Sequence[str]] = None,
        continue_on_error: bool = False,
    ) -> None:
        """Create an unloader.

        ``continue_on_error`` defaults to False: the first object error
        aborts the run, remaining types and schemas are not started, and
        the failed type's watermark is not moved.
        """
        if max_workers < 1:
            raise ValueError('max_workers must be >= 1')
        self.engine = engine
        self.schemas = _normalize_schemas(schemas)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self.object_types = normalize_object_types(object_types)
        self.skip_objects = normalize_skip_objects(skip_objects)
        self.continue_on_error = continue_on_error

    def ddl(self, *, full: bool = False, lookback_minutes: int = 0) -> RunStats:
        """Incremental object DDL into ``schema/<type>/*.sql``."""
        started = time.perf_counter()
        stats = RunStats()
        logger.info(
            'ddl start schemas=%s object_types=%s output_dir=%s full=%s '
            'lookback_minutes=%s',
            ','.join(self.schemas),
            ','.join(self.object_types),
            self.output_dir,
            full,
            lookback_minutes,
        )
        try:
            self._ddl(stats, full=full, lookback_minutes=lookback_minutes)
        except Exception as exc:
            self._fail(stats, started, exc, label='ddl')
        elapsed = time.perf_counter() - started
        logger.info(
            'ddl complete added=%d updated=%d deleted=%d skipped=%d failed=%d in %.1fs',
            stats.added,
            stats.updated,
            stats.deleted,
            stats.skipped,
            stats.failed,
            elapsed,
        )
        return stats

    def dependencies(self) -> int:
        """Full-refresh per-object trees under ``schema/_deps/``."""
        started = time.perf_counter()
        try:
            return dump_dependencies(
                self.engine,
                self.output_dir,
                self.schemas,
                self.object_types,
                max_workers=self.max_workers,
            )
        except Exception as exc:
            self._fail(RunStats(), started, exc, label='dependencies')

    def custom_sql(self) -> int:
        """Run ``_meta/`` SELECT scripts into ``schema/_meta/*.md``."""
        started = time.perf_counter()
        try:
            return dump_custom_sql(
                self.engine,
                self.output_dir,
                self.schemas,
                max_workers=self.max_workers,
            )
        except Exception as exc:
            self._fail(RunStats(), started, exc, label='custom sql')

    def _ddl(self, stats: RunStats, *, full: bool, lookback_minutes: int) -> None:
        for schema in self.schemas:
            logger.info('unloading schema %s to %s', schema, self.output_dir)
            unload_schema(
                self.engine,
                schema,
                self.output_dir,
                object_types=self.object_types,
                lookback_minutes=lookback_minutes,
                max_workers=self.max_workers,
                full=full,
                stats=stats,
                skip_objects=self.skip_objects,
                continue_on_error=self.continue_on_error,
            )
            logger.info('schema %s: done', schema)
        if stats.failures:
            preview = ', '.join(stats.failures[:10])
            if len(stats.failures) > 10:
                preview += f', ... ({len(stats.failures)} total)'
            raise UnloadError(
                f'failed to unload {len(stats.failures)} object(s): {preview}'
            )

    def _fail(
        self,
        stats: RunStats,
        started: float,
        exc: Exception,
        *,
        label: str,
    ) -> NoReturn:
        elapsed = time.perf_counter() - started
        logger.error(
            '%s failed added=%d updated=%d deleted=%d skipped=%d failed=%d (%.1fs)',
            label,
            stats.added,
            stats.updated,
            stats.deleted,
            stats.skipped,
            stats.failed,
            elapsed,
        )
        if isinstance(exc, UnloadError):
            raise
        raise UnloadError(str(exc)) from exc
