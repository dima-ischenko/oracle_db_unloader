from __future__ import annotations

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .inventory import (
    TYPE_FOLDERS,
    TYPE_SUFFIX,
    DbObject,
    fetch_inventory,
)
from .logger import get_logger
from .resources import load_sql

logger = get_logger(__name__)


@dataclass(frozen=True)
class Node:
    owner: str
    name: str
    type: str

    @property
    def label(self) -> str:
        return f'{self.owner.lower()}.{self.name.lower()}({self.type.lower()})'


@dataclass(frozen=True)
class DepRow:
    xref_path: str
    ref_type: str


@dataclass(frozen=True)
class DependencyGraph:
    dependents: Dict[Node, Tuple[Node, ...]]
    references: Dict[Node, Tuple[Node, ...]]


def node_from_object(obj: DbObject) -> Node:
    return Node(obj.owner, obj.name, obj.object_type)


def build_graph(edges: Iterable[Tuple[Node, Node]]) -> DependencyGraph:
    dependents: Dict[Node, List[Node]] = defaultdict(list)
    references: Dict[Node, List[Node]] = defaultdict(list)
    seen: Set[Tuple[Node, Node]] = set()
    for depender, referenced in edges:
        pair = (depender, referenced)
        if pair in seen:
            continue
        seen.add(pair)
        references[depender].append(referenced)
        dependents[referenced].append(depender)
    return DependencyGraph(
        dependents={key: tuple(value) for key, value in dependents.items()},
        references={key: tuple(value) for key, value in references.items()},
    )


def fetch_dependency_edges(engine: Engine) -> List[Tuple[Node, Node]]:
    sql = load_sql('dependency_edges.sql')
    edges: List[Tuple[Node, Node]] = []
    with engine.connect() as conn:
        for owner, name, typ, ref_owner, ref_name, ref_type in conn.execute(text(sql)):
            edges.append(
                (
                    Node(str(owner), str(name), str(typ)),
                    Node(str(ref_owner), str(ref_name), str(ref_type)),
                )
            )
    return edges


def dependency_rows(graph: DependencyGraph, root: Node) -> List[DepRow]:
    rows = _walk_upstream(graph, root)
    rows.extend(_walk_downstream(graph, root))
    rows.sort(key=lambda row: (row.ref_type != 'downstream', row.xref_path))
    return rows


def format_dep_markdown(rows: Sequence[DepRow]) -> str:
    sections: List[str] = []
    for title, ref_type in (('Downstream', 'downstream'), ('Upstream', 'upstream')):
        paths = [row.xref_path for row in rows if row.ref_type == ref_type]
        if not paths:
            continue
        sections.append(f'## {title}\n\n' + '\n'.join(paths) + '\n')
    return '\n'.join(sections)


def _neighbors(mapping: Dict[Node, Tuple[Node, ...]], node: Node) -> Tuple[Node, ...]:
    return mapping.get(node, ())


def _walk_upstream(graph: DependencyGraph, root: Node) -> List[DepRow]:
    rows: List[DepRow] = []
    for first in _neighbors(graph.dependents, root):
        stack: List[Tuple[Node, Tuple[Node, ...]]] = [(first, (first,))]
        while stack:
            current, path = stack.pop()
            kids = tuple(
                kid
                for kid in _neighbors(graph.dependents, current)
                if kid not in path and kid != root
            )
            if kids:
                for kid in kids:
                    stack.append((kid, path + (kid,)))
                continue
            rows.append(
                DepRow(
                    xref_path=_format_xref(reversed(path), toward_root=True),
                    ref_type='upstream',
                )
            )
    return rows


def _walk_downstream(graph: DependencyGraph, root: Node) -> List[DepRow]:
    rows: List[DepRow] = []
    for first in _neighbors(graph.references, root):
        stack: List[Tuple[Node, Tuple[Node, ...]]] = [(first, (first,))]
        while stack:
            current, path = stack.pop()
            kids = tuple(
                kid
                for kid in _neighbors(graph.references, current)
                if kid not in path and kid != root
            )
            if kids:
                for kid in kids:
                    stack.append((kid, path + (kid,)))
                continue
            rows.append(
                DepRow(
                    xref_path=_format_xref(path, toward_root=False),
                    ref_type='downstream',
                )
            )
    return rows


def _format_xref(nodes: Iterable[Node], *, toward_root: bool) -> str:
    """Omit the file's own object; mark direction with a leading or trailing '>'."""
    body = ' > '.join(node.label for node in nodes)
    if toward_root:
        return f'{body} >'
    return f'> {body}'


def walk_deps_files(
    output_dir: Path,
    schema: str,
    object_types: Sequence[str],
) -> Set[str]:
    found: Set[str] = set()
    for object_type in object_types:
        found.update(_walk_type_deps_files(output_dir, schema, object_type))
    return found


def _walk_type_deps_files(output_dir: Path, schema: str, object_type: str) -> Set[str]:
    folder = TYPE_FOLDERS[object_type]
    directory = Path(output_dir) / schema.lower() / '_deps' / folder
    if not directory.is_dir():
        return set()
    suffix = TYPE_SUFFIX.get(object_type)
    sibling_suffixes = [
        TYPE_SUFFIX[other]
        for other, other_folder in TYPE_FOLDERS.items()
        if other_folder == folder and other in TYPE_SUFFIX and other != object_type
    ]
    found: Set[str] = set()
    for path in directory.glob('*.md'):
        name = path.name.lower()
        if suffix:
            if not name.endswith(f'{suffix}.md'):
                continue
        elif any(name.endswith(f'{item}.md') for item in sibling_suffixes):
            continue
        found.add(path.relative_to(output_dir).as_posix())
    return found


def _write_object_deps(
    output_dir: Path, obj: DbObject, graph: DependencyGraph
) -> Tuple[Optional[str], int]:
    rows = dependency_rows(graph, node_from_object(obj))
    if not rows:
        return None, 0
    relpath = obj.deps_relpath
    target = Path(output_dir) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_dep_markdown(rows), encoding='utf-8')
    return relpath, len(rows)


def _write_dep_objects(
    output_dir: Path,
    schema: str,
    to_process: Sequence[DbObject],
    graph: DependencyGraph,
    workers: int,
) -> Tuple[Set[str], int]:
    remaining = len(to_process)
    iterator = iter(to_process)
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    known: Set[str] = set()
    written = 0
    try:
        for _ in range(workers):
            obj = next(iterator, None)
            if obj is None:
                break
            futures[pool.submit(_write_object_deps, output_dir, obj, graph)] = obj
        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                obj = futures.pop(future)
                try:
                    relpath, row_count = future.result()
                except Exception as exc:
                    logger.exception(
                        'failed to write dependencies %s %s.%s',
                        obj.object_type,
                        obj.owner,
                        obj.name,
                    )
                    raise RuntimeError(
                        f'failed to write dependencies {obj.object_type} '
                        f'{obj.owner}.{obj.name}'
                    ) from exc
                remaining -= 1
                if relpath is not None:
                    known.add(relpath)
                    written += 1
                    logger.info(
                        'dependencies %s schema=%s type=%s rows=%d remaining=%d',
                        (Path(output_dir) / relpath).as_posix(),
                        schema,
                        obj.object_type,
                        row_count,
                        remaining,
                    )
                nxt = next(iterator, None)
                if nxt is not None:
                    futures[
                        pool.submit(_write_object_deps, output_dir, nxt, graph)
                    ] = nxt
    finally:
        for future in list(futures):
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    return known, written


def write_schema_dependencies(
    output_dir: Path,
    schema: str,
    inventory: Sequence[DbObject],
    object_types: Sequence[str],
    graph: DependencyGraph,
    *,
    max_workers: int = 5,
) -> int:
    selected = set(object_types)
    to_process = [obj for obj in inventory if obj.object_type in selected]
    workers = max(1, min(max_workers, len(to_process) or 1))
    logger.info(
        'dependencies schema %s: inventory=%d selected=%d workers=%d',
        schema,
        len(inventory),
        len(to_process),
        workers,
    )
    known: Set[str] = set()
    written = 0
    if to_process:
        known, written = _write_dep_objects(
            output_dir, schema, to_process, graph, workers
        )

    stale = walk_deps_files(output_dir, schema, object_types) - known
    deleted = 0
    for relpath in sorted(stale):
        path = Path(output_dir) / relpath
        if path.is_file():
            path.unlink()
            deleted += 1
            logger.info('deleted %s schema=%s', path.as_posix(), schema)
    logger.info(
        'dependencies schema %s: write=%d delete=%d',
        schema,
        written,
        deleted,
    )
    return written


def dump_dependencies(
    engine: Engine,
    output_dir: Path,
    schemas: Sequence[str],
    object_types: Sequence[str],
    *,
    max_workers: int = 5,
) -> int:
    schema_list = [schema.strip().upper() for schema in schemas if schema.strip()]
    if not schema_list:
        return 0
    edges = fetch_dependency_edges(engine)
    logger.info(
        'dependencies: loaded %d edge(s) for %d schema(s) max_workers=%d',
        len(edges),
        len(schema_list),
        max_workers,
    )
    graph = build_graph(edges)
    dumped = 0
    for schema in schema_list:
        inventory = fetch_inventory(engine, schema)
        dumped += write_schema_dependencies(
            output_dir,
            schema,
            inventory,
            object_types,
            graph,
            max_workers=max_workers,
        )
    logger.info('dependencies: wrote %d file(s)', dumped)
    return dumped
