"""Console data-source aggregation for the primary run DB and temp eval DBs."""

from __future__ import annotations

import logging
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
    """Dynamically expose isolated ``/private/tmp`` evaluation runs read-only."""

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
        self._external: dict[Path, ConsoleDataSource] = {}

    def _discover_paths(self) -> set[Path]:
        root = self.temp_root
        if root is None or not root.is_dir():
            return set()
        paths: set[Path] = set()
        for path in root.glob("clickclick*/**/clickclick.db"):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_file() or root not in resolved.parents:
                continue
            if resolved == self.primary.db.path.resolve():
                continue
            paths.add(resolved)
        return paths

    def refresh(self) -> None:
        """Open new temp DBs read-only and forget DBs whose files were deleted."""
        discovered = self._discover_paths()
        for path in set(self._external) - discovered:
            self._external.pop(path).db.close()
        for path in sorted(discovered - set(self._external)):
            db: Database | None = None
            try:
                db = Database(path, read_only=True)
                # Fail closed on unrelated/corrupt SQLite files before cataloging.
                db.list_tasks()
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip unreadable temp run DB %s: %s", path, exc)
                try:
                    if db is not None:
                        db.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            relative = path.relative_to(self.temp_root).parent.as_posix()
            self._external[path] = ConsoleDataSource(
                db=db,
                artifacts=ArtifactStore(path.parent / "artifacts", create=False),
                label=f"temp/{relative}",
                read_only=True,
            )

    def sources(self) -> list[ConsoleDataSource]:
        self.refresh()
        return [self.primary, *self._external.values()]

    def list_tasks(self, status: TaskStatus | None = None) -> list[tuple[TaskRecord, ConsoleDataSource]]:
        """Return all tasks newest-first, with the primary DB winning UUID collisions."""
        out: list[tuple[TaskRecord, ConsoleDataSource]] = []
        seen: set[str] = set()
        for source in self.sources():
            try:
                tasks = source.db.list_tasks(status)
            except Exception as exc:  # noqa: BLE001
                logger.debug("skip unreadable Console source %s: %s", source.label, exc)
                continue
            for task in tasks:
                if task.id in seen:
                    continue
                seen.add(task.id)
                out.append((task, source))
        return sorted(out, key=lambda pair: pair[0].created_at, reverse=True)

    def locate(self, task_id: str) -> tuple[TaskRecord, ConsoleDataSource] | None:
        for source in self.sources():
            try:
                task = source.db.get_task(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("task lookup failed in %s: %s", source.label, exc)
                continue
            if task is not None:
                return task, source
        return None

    @staticmethod
    def decorate(payload: dict, source: ConsoleDataSource) -> dict:
        return {
            **payload,
            "read_only": source.read_only,
            "data_source": source.label,
        }

    def resolve_artifact(self, ref: str) -> Path | None:
        """Resolve a relative artifact ref across primary then temp stores."""
        for source in self.sources():
            path = source.artifacts.resolve(ref)
            if path.is_file():
                return path
        return None

    def close(self) -> None:
        for source in self._external.values():
            source.db.close()
        self._external.clear()
