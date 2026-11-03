from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evebench.domain.models import JSONValue, json_safe

CONFIG_VERSION = 1


@dataclass(frozen=True, slots=True)
class BackendBenchmarkConfig:
    options: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    name: str
    dataset_path: Path
    database_path: Path
    timeout_ms: int
    semantics: str
    seed: int | None
    checker_ids: tuple[str, ...]
    backends: dict[str, BackendBenchmarkConfig]
    max_workers: int
    max_tasks_per_worker: int
    progress_every: int
    strip_shared_order_by_limit: bool = False
    source_path: Path | None = None
    version: int = CONFIG_VERSION

    @classmethod
    def load(cls, path: str | Path) -> BenchmarkConfig:
        source = Path(path).resolve()
        with source.open("rb") as stream:
            raw = tomllib.load(stream)
        return cls._from_mapping(raw, source_path=source)

    @classmethod
    def ad_hoc(
        cls,
        dataset_path: str | Path,
        *,
        database_path: str | Path = "data/evebench.sqlite",
    ) -> BenchmarkConfig:
        dataset = Path(dataset_path).resolve()
        return cls(
            name=dataset.stem,
            dataset_path=dataset,
            database_path=Path(database_path).resolve(),
            timeout_ms=30_000,
            semantics="bag",
            seed=1,
            checker_ids=(),
            backends={},
            max_workers=4,
            max_tasks_per_worker=25,
            progress_every=10,
        )

    @classmethod
    def _from_mapping(
        cls,
        raw: dict[str, Any],
        *,
        source_path: Path,
    ) -> BenchmarkConfig:
        allowed = {
            "version",
            "name",
            "dataset",
            "database",
            "timeout_ms",
            "semantics",
            "seed",
            "checkers",
            "max_workers",
            "max_tasks_per_worker",
            "progress_every",
            "strip_shared_order_by_limit",
            "backends",
        }
        _reject_unknown(raw, allowed, "benchmark")
        version = _integer(raw, "version")
        if version != CONFIG_VERSION:
            raise ValueError(
                f"unsupported benchmark config version {version}; "
                f"expected {CONFIG_VERSION}"
            )
        name = _nonempty_string(raw, "name")
        dataset_path = _relative_path(source_path, _nonempty_string(raw, "dataset"))
        if not dataset_path.is_file():
            raise ValueError(f"benchmark dataset does not exist: {dataset_path}")
        database_path = _relative_path(source_path, _nonempty_string(raw, "database"))
        timeout_ms = _integer(raw, "timeout_ms")
        if not 100 <= timeout_ms <= 300_000:
            raise ValueError("timeout_ms must be between 100 and 300000")
        semantics = _nonempty_string(raw, "semantics").lower()
        if semantics not in {"bag", "set"}:
            raise ValueError("semantics must be 'bag' or 'set'")
        seed = raw.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("seed must be an integer or omitted")
        checker_ids = _string_tuple(raw, "checkers")
        if not checker_ids:
            raise ValueError("checkers must contain at least one checker ID")
        if len(set(checker_ids)) != len(checker_ids):
            raise ValueError("checkers must not contain duplicates")

        backend_raw = raw.get("backends", {})
        if not isinstance(backend_raw, dict):
            raise ValueError("backends must be a table")
        backends: dict[str, BackendBenchmarkConfig] = {}
        for checker_id, value in backend_raw.items():
            if not isinstance(value, dict):
                raise ValueError(f"backends.{checker_id} must be a table")
            _reject_unknown(
                value,
                {"options"},
                f"backends.{checker_id}",
            )
            options = value.get("options", {})
            if not isinstance(options, dict):
                raise ValueError(f"backends.{checker_id}.options must be a table")
            backends[str(checker_id)] = BackendBenchmarkConfig(
                options={str(key): json_safe(item) for key, item in options.items()},
            )
        max_workers = _integer(raw, "max_workers")
        max_tasks = _integer(raw, "max_tasks_per_worker")
        progress_every = _integer(raw, "progress_every")
        strip_shared_order_by_limit = raw.get("strip_shared_order_by_limit", False)
        if not isinstance(strip_shared_order_by_limit, bool):
            raise ValueError("strip_shared_order_by_limit must be a boolean")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_tasks < 0:
            raise ValueError("max_tasks_per_worker must be non-negative")
        if progress_every < 1:
            raise ValueError("progress_every must be positive")
        return cls(
            version=version,
            name=name,
            dataset_path=dataset_path,
            database_path=database_path,
            timeout_ms=timeout_ms,
            semantics=semantics,
            seed=seed,
            checker_ids=checker_ids,
            backends=backends,
            max_workers=max_workers,
            max_tasks_per_worker=max_tasks,
            progress_every=progress_every,
            strip_shared_order_by_limit=strip_shared_order_by_limit,
            source_path=source_path,
        )

    def backend_options(self) -> dict[str, dict[str, JSONValue]]:
        return {
            checker_id: dict(config.options)
            for checker_id, config in self.backends.items()
        }


def _reject_unknown(
    value: dict[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {location} option(s): {', '.join(unknown)}")


def _nonempty_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return item.strip()


def _integer(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


def _string_tuple(value: dict[str, Any], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in item
    ):
        raise ValueError(f"{key} must be an array of non-empty strings")
    return tuple(entry.strip() for entry in item)


def _relative_path(source_path: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = source_path.parent / path
    return path.resolve()
