"""Console data-source aggregation for the primary run DB and isolated eval DBs."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from control_api.services import ObservabilityQueries
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import TaskRecord, TaskStatus


logger = logging.getLogger("control_api.data_sources")


@dataclass(frozen=True)
class ConsoleDataSource:
    """One Console-readable SQLite/artifact pair."""

    db: Database
    artifacts: ArtifactStore
    label: str
    read_only: bool

    @property
    def queries(self) -> ObservabilityQueries:
        return ObservabilityQueries(self.db, self.artifacts)


class ConsoleDataSources:
    """Expose isolated evaluation DBs under ``/private/tmp`` and ``data/`` read-only."""

    def __init__(
        self,
        primary_db: Database,
        primary_artifacts: ArtifactStore,
        *,
        temp_root: Path | None,
    ) -> None:
        self.primary = ConsoleDataSource(
            db=primary_db,
            artifacts=primary_artifacts,
            label="primary",
            read_only=False,
        )
        self.temp_root = temp_root.resolve() if temp_root is not None else None
        self.data_root = primary_db.path.parent.resolve()
        self._external: dict[Path, ConsoleDataSource] = {}
        self._task_sources: dict[str, ConsoleDataSource] = {}
        self._next_discovery_at = 0.0
        # The web listing publishes immutable header snapshots. Its worker owns
        # separate short-lived read-only connections, never runtime connections.
        self._index_thread: threading.Thread | None = None
        self._index_stop = threading.Event()
        self._index_headers: dict[Path, tuple[dict, ...]] = {}
        self._index_cache: dict[Path, tuple[tuple, tuple[dict, ...]]] = {}
        self._index_ready = False
        self._index_failed = False
        self._next_index_at = 0.0

    def _accept(self, path: Path, root: Path) -> Path | None:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_file() or root not in resolved.parents:
            return None
        if resolved == self.primary.db.path.resolve():
            return None
        return resolved

    def _discover_temp_paths(self) -> set[Path]:
        root = self.temp_root
        if root is None or not root.is_dir():
            return set()
        paths: set[Path] = set()
        for path in root.glob("clickclick*/**/clickclick.db"):
            accepted = self._accept(path, root)
            if accepted is not None:
                paths.add(accepted)
        return paths

    def _discover_data_dir_paths(self) -> set[Path]:
        """Find isolated eval DBs below the data root without walking artifacts."""
        root = self.data_root
        if not root.is_dir():
            return set()
        paths: set[Path] = set()
        for current, dirnames, filenames in os.walk(root, topdown=True):
            if self._index_stop.is_set():
                break
            # Installed Python environments are dependencies, not run stores.
            if "pyvenv.cfg" in filenames:
                dirnames[:] = []
                continue
            # Evaluation layouts vary in depth. Prune artifact stores and hidden
            # metadata directories before descending so discovery stays cheap.
            dirnames[:] = [
                name for name in dirnames
                if name not in {
                    "artifacts", "node_modules", "site-packages", "__pycache__",
                    "latest-shallow", "skills-profile", "skills", "openspec",
                }
                and not name.startswith(".")
            ]
            if "clickclick.db" not in filenames:
                continue
            accepted = self._accept(Path(current) / "clickclick.db", root)
            if accepted is not None:
                paths.add(accepted)
        return paths

    def _discover_paths(self) -> set[Path]:
        return self._discover_temp_paths() | self._discover_data_dir_paths()

    def _label_for(self, path: Path) -> str:
        parent = path.parent
        if self.temp_root is not None:
            try:
                return f"temp/{parent.relative_to(self.temp_root).as_posix()}"
            except ValueError:
                pass
        try:
            return f"eval/{parent.relative_to(self.data_root).as_posix()}"
        except ValueError:
            return parent.as_posix()

    def refresh(self) -> None:
        """Open new isolated DBs read-only and forget DBs whose files were deleted."""
        discovered = self._discover_paths()
        for path in set(self._external) - discovered:
            removed = self._external.pop(path)
            self._task_sources = {
                task_id: source
                for task_id, source in self._task_sources.items()
                if source is not removed
            }
            removed.db.close()
        for path in sorted(discovered - set(self._external)):
            db: Database | None = None
            try:
                db = Database(path, read_only=True)
                # Fail closed on unrelated/corrupt SQLite files before cataloging.
                tasks = db.task_headers()
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip unreadable eval run DB %s: %s", path, exc)
                try:
                    if db is not None:
                        db.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            source = ConsoleDataSource(
                db=db,
                artifacts=ArtifactStore(path.parent / "artifacts", create=False),
                label=self._label_for(path),
                read_only=True,
            )
            self._external[path] = source
            for task in tasks:
                self._task_sources.setdefault(task["id"], source)
        self._next_discovery_at = time.monotonic() + 15.0

    def sources(self) -> list[ConsoleDataSource]:
        # Rediscover new eval directories periodically, but read task status live.
        # Walking the whole data tree on every poll blocks the API for seconds.
        if time.monotonic() >= self._next_discovery_at:
            self.refresh()
        return [self.primary, *self._external.values()]

    def list_tasks(
        self, status: TaskStatus | None = None, *, include_state: bool = True
    ) -> list[tuple[TaskRecord, ConsoleDataSource]]:
        """Return all tasks newest-first, with the primary DB winning UUID collisions."""
        out: list[tuple[TaskRecord, ConsoleDataSource]] = []
        seen: set[str] = set()
        for source in self.sources():
            try:
                tasks = source.db.list_tasks(status, include_state=include_state)
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip unreadable Console source %s: %s", source.label, exc)
                continue
            for task in tasks:
                if task.id in seen:
                    continue
                seen.add(task.id)
                self._task_sources[task.id] = source
                out.append((task, source))
        return sorted(out, key=lambda pair: pair[0].created_at, reverse=True)

    @property
    def indexing(self) -> bool:
        return self._index_thread is not None and self._index_thread.is_alive()

    @property
    def index_failed(self) -> bool:
        return self._index_failed

    @property
    def index_ready(self) -> bool:
        return self._index_ready

    @staticmethod
    def _index_signature(path: Path) -> tuple:
        # WAL writes may not touch the main database. Both files are needed.
        signature = []
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                stat = candidate.stat()
                signature.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                signature.append((0, 0))
        return tuple(signature)

    def _update_index(self) -> None:
        try:
            paths = self._discover_paths()
            signatures = {}
            for path in paths:
                try:
                    signatures[path] = self._index_signature(path)
                except OSError:
                    continue
            # Newest-written runs appear first while a cold index is incomplete.
            ordered = sorted(signatures, key=lambda p: (max(signatures[p]), str(p)), reverse=True)
            cache = {}
            headers = {}
            for index, path in enumerate(ordered):
                if self._index_stop.is_set():
                    return
                signature = signatures[path]
                previous = self._index_cache.get(path)
                if previous and previous[0] == signature:
                    rows = previous[1]
                else:
                    reader = None
                    try:
                        reader = Database(path, read_only=True)
                        rows = tuple(reader.task_headers())
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("skip index source %s: %s", path, exc)
                        continue
                    finally:
                        if reader is not None:
                            reader.close()
                cache[path] = (signature, rows)
                headers[path] = rows
                # Publish copies: API readers never iterate a mutating mapping.
                # A refresh keeps the previous complete view until replacement.
                if not self._index_ready and (index == 0 or index % 25 == 0):
                    self._index_headers = dict(headers)
            if not self._index_stop.is_set():
                self._index_headers = headers
                self._index_cache = cache
                self._index_ready = True
                self._index_failed = False
        except Exception:  # noqa: BLE001
            self._index_failed = True
            logger.exception("Console task index refresh failed")
        finally:
            self._next_index_at = time.monotonic() + 15.0

    def _start_index(self) -> None:
        if self._index_stop.is_set() or self.indexing or time.monotonic() < self._next_index_at:
            return
        self._index_thread = threading.Thread(
            target=self._update_index, name="console-task-index", daemon=True,
        )
        self._index_thread.start()

    def _indexed_source(self, path: Path) -> ConsoleDataSource:
        source = self._external.get(path)
        if source is None:
            source = ConsoleDataSource(
                db=Database(path, read_only=True),
                artifacts=ArtifactStore(path.parent / "artifacts", create=False),
                label=self._label_for(path), read_only=True,
            )
            self._external[path] = source
        return source

    def _retire_index_sources(self) -> None:
        if not self._index_ready:
            return
        for path in set(self._external) - self._index_headers.keys():
            source = self._external.pop(path)
            self._task_sources = {key: value for key, value in self._task_sources.items()
                                  if value is not source}
            source.db.close()

    def task_page(self, *, page: int, page_size: int, status: TaskStatus | None = None,
                  background: bool = False):
        """Merge lightweight IDs globally, then load only the selected page.

        Resolve collisions before filtering: an external stale failure must not
        replace a primary task that has since succeeded.
        """
        catalog = {}
        if background:
            self._start_index()
            self._retire_index_sources()
            for header in self.primary.db.task_headers():
                catalog[header["id"]] = (header, self.primary)
            for path, headers in sorted(self._index_headers.items()):
                for header in headers:
                    catalog.setdefault(header["id"], (header, path))
        else:
            for source in self.sources():
                try:
                    for header in source.db.task_headers():
                        catalog.setdefault(header["id"], (header, source))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("skip unreadable Console source %s: %s", source.label, exc)
        ordered = sorted(
            (pair for pair in catalog.values() if status is None or pair[0]["status"] == status.value),
            key=lambda pair: (pair[0]["created_at"], pair[0]["id"]), reverse=True,
        )
        total = len(ordered)
        page = min(page, max(1, (total + page_size - 1) // page_size))
        selected = ordered[(page - 1) * page_size:page * page_size]
        items = []
        for header, source in selected:
            try:
                if isinstance(source, Path):
                    source = self._indexed_source(source)
                task = source.db.get_task_summary(header["id"])
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip unreadable task %s: %s", header["id"], exc)
                continue
            if task is not None:
                self._task_sources[task.id] = source
                items.append((task, source))
        return items, total, page

    def locate(self, task_id: str) -> tuple[TaskRecord, ConsoleDataSource] | None:
        self._retire_index_sources()
        # The primary database always wins UUID collisions and can gain tasks
        # after startup, so check it before consulting the external-source cache.
        try:
            primary_task = self.primary.db.get_task(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("primary task lookup failed: %s", exc)
        else:
            if primary_task is not None:
                self._task_sources[task_id] = self.primary
                return primary_task, self.primary
        cached = self._task_sources.get(task_id)
        if cached is not None:
            try:
                task = cached.db.get_task(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("cached task lookup failed in %s: %s", cached.label, exc)
            else:
                if task is not None:
                    return task, cached
            self._task_sources.pop(task_id, None)
        for path, headers in self._index_headers.items():
            if any(header["id"] == task_id for header in headers):
                try:
                    source = self._indexed_source(path)
                    task = source.db.get_task(task_id)
                except Exception:  # noqa: BLE001
                    continue
                if task is not None:
                    self._task_sources[task_id] = source
                    return task, source
        if self._index_ready:
            # A missing/deleted task must not trigger another synchronous scan.
            return None
        for source in self.sources()[1:]:
            try:
                task = source.db.get_task(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("task lookup failed in %s: %s", source.label, exc)
                continue
            if task is not None:
                self._task_sources[task_id] = source
                return task, source
        return None

    @staticmethod
    def decorate(payload: dict, source: ConsoleDataSource) -> dict:
        return {
            **payload,
            "read_only": source.read_only,
            "data_source": source.label,
        }

    def resolve_artifact(self, ref: str, *, task_id: str | None = None) -> Path | None:
        """Resolve a relative artifact ref across primary then temp stores."""
        if task_id is not None:
            located = self.locate(task_id)
            if located is None:
                return None
            _task, source = located
            path = source.artifacts.resolve(ref)
            return path if path.is_file() else None
        for source in self.sources():
            path = source.artifacts.resolve(ref)
            if path.is_file():
                return path
        return None

    def close(self) -> None:
        self._index_stop.set()
        if self._index_thread is not None:
            self._index_thread.join(timeout=1)
        for source in self._external.values():
            source.db.close()
        self._external.clear()
        self._task_sources.clear()
        self._next_discovery_at = 0.0
