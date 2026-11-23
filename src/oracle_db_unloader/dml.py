from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine
from tabulate import tabulate

from .logger import get_logger
from .resources import bind_params

logger = get_logger(__name__)

NULL_TOKEN = 'null'
CUSTOM_SQL_FOLDER = '_meta'
_MONTHS = (
    'jan',
    'feb',
    'mar',
    'apr',
    'may',
    'jun',
    'jul',
    'aug',
    'sep',
    'oct',
    'nov',
    'dec',
)


def stringify_cell(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        month = _MONTHS[value.month - 1]
        return f'{value.day:02d}-{month}-{value.year} {value:%H:%M:%S}'
    if isinstance(value, date):
        month = _MONTHS[value.month - 1]
        return f'{value.day:02d}-{month}-{value.year}'
    if isinstance(value, Decimal):
        rendered = format(value, 'f')
        if '.' in rendered:
            rendered = rendered.rstrip('0').rstrip('.')
        return rendered
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def format_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> str:
    str_rows: List[List[str]] = []
    for row in rows:
        rendered_row = []
        for value in row:
            rendered = stringify_cell(value)
            rendered_row.append(NULL_TOKEN if rendered is None else rendered)
        str_rows.append(rendered_row)
    return (
        tabulate(
            str_rows,
            headers=list(columns),
            tablefmt='github',
            disable_numparse=True,
        )
        + '\n'
    )


def discover_custom_sql(root: Path) -> List[Path]:
    """Find ``_meta/*.sql`` scripts under ``root``."""
    meta = Path(root) / CUSTOM_SQL_FOLDER
    if not meta.is_dir():
        return []
    return sorted(path for path in meta.glob('*.sql') if path.is_file())


def discover_schema_meta_sql(schema_root: Path) -> List[Path]:
    """Find schema-specific scripts in ``<schema>/_meta/*.sql``."""
    meta = Path(schema_root) / CUSTOM_SQL_FOLDER
    if not meta.is_dir():
        return []
    return sorted(path for path in meta.glob('*.sql') if path.is_file())


def _warn_top_level_sql(root: Path) -> None:
    stray = sorted(path for path in Path(root).glob('*.sql') if path.is_file())
    if not stray:
        return
    example = stray[0].name
    logger.warning(
        'custom sql ignored top-level %s under %s; put scripts in _<dir>/ '
        '(e.g. _meta/%s)',
        ', '.join(path.name for path in stray),
        root,
        example,
    )


def _schema_meta_scripts(root: Path, schema: str) -> Dict[str, Path]:
    if not root.is_dir():
        return {}
    return {path.stem: path for path in discover_schema_meta_sql(root / schema.lower())}


@dataclass(frozen=True)
class _SqlJob:
    path: Path
    schema: str
    target: Path


def _run_custom_sql(engine: Engine, job: _SqlJob) -> Optional[int]:
    sql = job.path.read_text(encoding='utf-8').strip()
    if not sql:
        logger.warning('custom sql skipped empty file %s', job.path.as_posix())
        return None
    params = bind_params(sql, {'schema': job.schema})
    with engine.connect() as conn:
        result = conn.execute(text(sql), params)
        columns = list(result.keys())
        rows = [list(row) for row in result]
    job.target.parent.mkdir(parents=True, exist_ok=True)
    job.target.write_text(format_table(columns, rows), encoding='utf-8')
    return len(rows)


def _run_custom_sql_jobs(
    engine: Engine,
    jobs: Sequence[_SqlJob],
    workers: int,
) -> int:
    remaining = len(jobs)
    iterator = iter(jobs)
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    dumped = 0
    try:
        for _ in range(workers):
            job = next(iterator, None)
            if job is None:
                break
            futures[pool.submit(_run_custom_sql, engine, job)] = job
        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                job = futures.pop(future)
                try:
                    row_count = future.result()
                except Exception as exc:
                    logger.exception(
                        'failed custom sql %s schema=%s',
                        job.path.name,
                        job.schema,
                    )
                    raise RuntimeError(
                        f'failed custom sql {job.path.name} schema={job.schema}'
                    ) from exc
                remaining -= 1
                if row_count is not None:
                    dumped += 1
                    logger.info(
                        'custom sql %s schema=%s -> %s (%d rows) remaining=%d',
                        job.path.name,
                        job.schema,
                        job.target.as_posix(),
                        row_count,
                        remaining,
                    )
                nxt = next(iterator, None)
                if nxt is not None:
                    futures[pool.submit(_run_custom_sql, engine, nxt)] = nxt
    finally:
        for future in list(futures):
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    return dumped


def dump_custom_sql(
    engine: Engine,
    output_dir: Path,
    schemas: Sequence[str],
    *,
    max_workers: int = 5,
) -> int:
    target_root = Path(output_dir)
    _warn_top_level_sql(target_root)
    shared = discover_custom_sql(target_root)
    schema_list = [schema.strip().upper() for schema in schemas if schema.strip()]
    per_schema = {
        schema: _schema_meta_scripts(target_root, schema) for schema in schema_list
    }
    schema_script_count = sum(len(items) for items in per_schema.values())
    if not shared and schema_script_count == 0:
        logger.info('custom sql: 0 script(s)')
        return 0

    jobs: List[_SqlJob] = []
    for path in shared:
        stem = path.stem
        for schema in schema_list:
            if stem in per_schema[schema]:
                continue
            jobs.append(
                _SqlJob(
                    path=path,
                    schema=schema,
                    target=(
                        target_root / schema.lower() / CUSTOM_SQL_FOLDER / f'{stem}.md'
                    ),
                )
            )
    for schema in schema_list:
        for stem, path in per_schema[schema].items():
            jobs.append(
                _SqlJob(
                    path=path,
                    schema=schema,
                    target=(
                        target_root / schema.lower() / CUSTOM_SQL_FOLDER / f'{stem}.md'
                    ),
                )
            )
    workers = max(1, min(max_workers, len(jobs) or 1))
    logger.info(
        'custom sql: %d shared script(s), %d schema-specific for %d schema(s) '
        'jobs=%d workers=%d',
        len(shared),
        schema_script_count,
        len(schema_list),
        len(jobs),
        workers,
    )
    dumped = _run_custom_sql_jobs(engine, jobs, workers) if jobs else 0
    logger.info('custom sql: wrote %d file(s)', dumped)
    return dumped
