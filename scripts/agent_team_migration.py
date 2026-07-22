#!/usr/bin/env python3
"""Migrate a target-local Agent-Team overlay into an independent AX_ROOT.

The migration is intentionally conservative.  Legacy SQLite bytes and artifacts
are copied as immutable evidence, ambiguous activations are quarantined for PL
reissue, and rollback restores only the atomic control-plane pointer.  It never
rewrites or removes the target checkout or the preserved legacy evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

try:
    from scripts.agent_team_git import ManagedRepositoryService
    from scripts.agent_team_layout import AgentTeamLayout
    from scripts.agent_team_paths import AxPathAuthority, AxPathError
    from scripts.agent_team_state import AxStateStore
except ModuleNotFoundError:  # Direct execution from a materialized bundle.
    from agent_team_git import ManagedRepositoryService
    from agent_team_layout import AgentTeamLayout
    from agent_team_paths import AxPathAuthority, AxPathError
    from agent_team_state import AxStateStore


SCHEMA_VERSION = 4
TOPOLOGY_ID = "six-slot-v1"
MAX_THREADS = 6
DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"
TERMINAL_LEGACY_ACTIVATION_STATES = frozenset(
    {"TERMINATED", "RESOURCES_RELEASED", "RECOVERY_CLEANED"}
)
REQUIRED_MCP = {
    "serena": {
        "enabled": True,
        "required": True,
        "required_tool": "initial_instructions",
        "fallback_allowed": False,
    },
    "sequentialthinking": {
        "enabled": True,
        "required": True,
        "fallback_allowed": False,
    },
}


class MigrationError(RuntimeError):
    """Raised when migration planning or execution would be unsafe."""


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    source: str
    target: str
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    migration_id: str
    target_id: str
    source_ref: str
    source_oid: str
    target_checkout: Path
    legacy_root: Path
    ax_root: Path
    evidence_files: tuple[EvidenceFile, ...]
    quarantined_activation_ids: tuple[str, ...]
    preserved_activation_ids: tuple[str, ...]
    migration_manifest: Mapping[str, Any]
    deletion_manifest: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "target_id": self.target_id,
            "source_ref": self.source_ref,
            "source_oid": self.source_oid,
            "target_checkout": str(self.target_checkout),
            "legacy_root": str(self.legacy_root),
            "ax_root": str(self.ax_root),
            "evidence_files": [asdict(item) for item in self.evidence_files],
            "quarantined_activation_ids": list(
                self.quarantined_activation_ids
            ),
            "preserved_activation_ids": list(self.preserved_activation_ids),
            "pl_reissue_required": bool(self.quarantined_activation_ids),
            "migration_manifest": dict(self.migration_manifest),
            "deletion_manifest": dict(self.deletion_manifest),
        }


@dataclass(frozen=True, slots=True)
class MigrationResult:
    operation: str
    migration_id: str
    target_id: str
    ax_root: Path
    managed_repository: Path
    source_oid: str
    evidence_file_count: int
    quarantined_activation_count: int
    pointer_path: Path
    verified: bool
    cut_over: bool
    rolled_back: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in ("ax_root", "managed_repository", "pointer_path"):
            value[field] = str(value[field])
        return value


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _open_source_reader(path: Path):
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = (
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x80000000 | 0x00000100,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00000080,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            raise OSError(error, f"could not open source without atime: {path}")
        unchanged = FileTime(0xFFFFFFFF, 0xFFFFFFFF)
        set_file_time = kernel32.SetFileTime
        set_file_time.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.c_void_p,
        )
        set_file_time.restype = wintypes.BOOL
        if not set_file_time(
            handle,
            None,
            ctypes.byref(unchanged),
            None,
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "could not suppress source last-access updates")
        try:
            descriptor = msvcrt.open_osfhandle(
                handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        return os.fdopen(descriptor, "rb")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_atime = getattr(os, "O_NOATIME", None)
    if no_atime is None:
        raise OSError(
            "platform cannot guarantee source-preserving no-atime reads"
        )
    descriptor = os.open(path, flags | no_atime)
    return os.fdopen(descriptor, "rb")


def _read_source_bytes(path: Path) -> bytes:
    with _open_source_reader(path) as stream:
        return stream.read()


def _source_metadata(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_mode,
        metadata.st_size,
        metadata.st_atime_ns,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _copy_source_file(source: Path, destination: Path) -> None:
    before = _source_metadata(source)
    with _open_source_reader(source) as source_stream:
        with destination.open("wb") as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
    if _source_metadata(source) != before:
        raise MigrationError(f"source metadata changed while reading: {source}")


def _sha256_file(path: Path, *, preserve_access_time: bool = False) -> str:
    digest = hashlib.sha256()
    stream = _open_source_reader(path) if preserve_access_time else path.open("rb")
    with stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _manifest_with_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("manifest_sha256", None)
    value["manifest_sha256"] = _sha256_bytes(_canonical_json(value))
    return value


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(left: Path, right: Path) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _run_git(checkout: Path, *arguments: str) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MigrationError(
            f"Git inspection failed ({arguments[0]}): {diagnostic[:1024]}"
        )
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _source_ref(checkout: Path, requested: str | None) -> str:
    value = requested or _run_git(checkout, "symbolic-ref", "--quiet", "HEAD")
    if not value.startswith("refs/heads/") or ".." in value or value.endswith("/"):
        raise MigrationError("source_ref must be a full refs/heads/... reference")
    return value


def _checkout_fingerprint(checkout: Path) -> str:
    untracked = _run_git(
        checkout, "ls-files", "--others", "--exclude-standard", "-z"
    )
    untracked_evidence: list[dict[str, str]] = []
    for relative in sorted(item for item in untracked.split("\x00") if item):
        path = (checkout / PurePosixPath(relative)).resolve()
        if not _is_within(checkout, path) or not path.is_file() or path.is_symlink():
            raise MigrationError(f"unsafe untracked target path: {relative}")
        untracked_evidence.append(
            {"path": relative, "sha256": _sha256_file(path)}
        )
    evidence = {
        "head": _run_git(checkout, "rev-parse", "HEAD"),
        "symbolic_head": _run_git(checkout, "symbolic-ref", "--quiet", "HEAD"),
        "status": _run_git(
            checkout, "status", "--porcelain=v1", "--untracked-files=all"
        ),
        "worktree_diff": _sha256_bytes(
            _run_git(checkout, "diff", "--binary").encode("utf-8")
        ),
        "index_diff": _sha256_bytes(
            _run_git(checkout, "diff", "--cached", "--binary").encode("utf-8")
        ),
        "untracked": untracked_evidence,
    }
    return _sha256_bytes(_canonical_json(evidence))


def _read_registry(layout: AgentTeamLayout) -> tuple[list[dict[str, Any]], list[str]]:
    path = layout.config_root / "seats" / "registry.toml"
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MigrationError(f"cannot read six-slot seat registry: {path}") from exc
    seats = document.get("seats")
    legacy = document.get("legacy")
    if not isinstance(seats, list) or len(seats) != 5 or not isinstance(legacy, dict):
        raise MigrationError("six-slot seat mapping is incomplete")
    expected_slots = {"pm_ta", "pl", "dev_1", "dev_2", "qa_build"}
    mappings = [
        {
            "target_slot_key": str(seat.get("slot_key")),
            "retained_seat_id": str(seat.get("seat_id")),
            "absorbed_seat_ids": list(seat.get("absorbed_seat_ids", [])),
            "legacy_slot_keys": list(seat.get("legacy_slot_keys", [])),
        }
        for seat in seats
    ]
    if {item["target_slot_key"] for item in mappings} != expected_slots:
        raise MigrationError("six-slot seat mapping does not cover fixed slots")
    absorbed = {
        item
        for mapping in mappings
        for item in mapping["absorbed_seat_ids"]
    }
    archived = list(legacy.get("archived_seat_ids", []))
    if absorbed != {"TA_권지호", "BUILD_RELEASE_정서준"} or archived != [
        "DEV_정예은"
    ]:
        raise MigrationError("legacy seat absorption/archive evidence is invalid")
    return sorted(mappings, key=lambda item: item["target_slot_key"]), archived


def _walk_regular_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            path = current_path / name
            if path.is_file() and not path.is_symlink():
                files.append(path)
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def _legacy_evidence_files(
    legacy_root: Path,
    ax_root: Path,
    migration_id: str,
) -> tuple[EvidenceFile, ...]:
    sources: list[tuple[Path, PurePosixPath]] = []
    database = legacy_root / "state" / "agent-team.db"
    for candidate in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if candidate.is_file() and not candidate.is_symlink():
            sources.append(
                (candidate, PurePosixPath("legacy/state") / candidate.name)
            )
    artifacts = legacy_root / "artifacts"
    for source in _walk_regular_files(artifacts):
        relative = PurePosixPath(*source.relative_to(artifacts).parts)
        sources.append((source, PurePosixPath("legacy/artifacts") / relative))
    destination_root = ax_root / "artifacts" / "migrations" / migration_id
    result = [
        EvidenceFile(
            source=str(source.resolve()),
            target=str(destination_root.joinpath(*relative.parts).resolve()),
            sha256=_sha256_file(source, preserve_access_time=True),
            byte_count=source.stat().st_size,
        )
        for source, relative in sources
    ]
    return tuple(sorted(result, key=lambda item: item.target))


def _read_legacy_activations(
    database: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not database.is_file() or database.is_symlink():
        return (), ()
    wal = database.with_name(database.name + "-wal")
    source_paths = tuple(path for path in (database, wal) if path.is_file())
    before = {path: _source_metadata(path) for path in source_paths}
    try:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-legacy-sqlite-observation-"
        ) as temporary:
            snapshot = Path(temporary) / database.name
            for source in source_paths:
                _copy_source_file(source, snapshot.with_name(source.name))
            after = {path: _source_metadata(path) for path in source_paths}
            if before != after or tuple(
                path for path in (database, wal) if path.is_file()
            ) != source_paths:
                raise MigrationError(
                    "legacy SQLite files changed during source-preserving observation"
                )
            uri = (
                f"file:{quote(snapshot.resolve().as_posix(), safe='/:')}?mode=ro"
            )
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='activations'"
                ).fetchone()
                if table is None:
                    return (), ()
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(activations)"
                    )
                }
                if not {"id", "state"} <= columns:
                    return ("legacy-activations-schema-ambiguous",), ()
                rows = connection.execute(
                    "SELECT id, state FROM activations ORDER BY id"
                ).fetchall()
    except sqlite3.Error:
        return ("legacy-activations-database-unreadable",), ()
    except OSError as exc:
        raise MigrationError(
            "legacy SQLite snapshot could not be observed without source writes"
        ) from exc
    quarantined = tuple(
        str(row["id"])
        for row in rows
        if str(row["state"]) not in TERMINAL_LEGACY_ACTIVATION_STATES
    )
    preserved = tuple(
        str(row["id"])
        for row in rows
        if str(row["state"]) in TERMINAL_LEGACY_ACTIVATION_STATES
    )
    return quarantined, preserved


def _safe_output_path(
    output: Path,
    *,
    protected_roots: Sequence[Path],
) -> Path:
    resolved = output.expanduser().resolve()
    if any(_is_within(root, resolved) for root in protected_roots):
        raise MigrationError(
            "dry-run output must be outside source, target, legacy, and AX roots"
        )
    if resolved.exists() and not resolved.is_file():
        raise MigrationError(f"manifest output is not a regular file: {resolved}")
    if resolved.is_symlink() or resolved.parent.is_symlink():
        raise MigrationError(f"manifest output cannot use a symlink: {resolved}")
    return resolved


def _atomic_write(path: Path, content: bytes, *, read_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.ax-migration-tmp")
    if temporary.exists():
        temporary.unlink()
    temporary.write_bytes(content)
    if read_only:
        try:
            os.chmod(temporary, stat.S_IREAD)
        except OSError:
            pass
    os.replace(temporary, path)


class AgentTeamMigrator:
    """Replay-safe migration controller for one target checkout and AX_ROOT."""

    def __init__(
        self,
        *,
        ax_source_root: Path,
        target_checkout: Path,
        legacy_root: Path,
        ax_root: Path,
        source_ref: str | None = None,
        repo_id: str | None = None,
    ) -> None:
        self.layout = AgentTeamLayout.discover(ax_source_root)
        self.ax_source_root = self.layout.source_root.resolve()
        self.target_checkout = Path(target_checkout).expanduser().resolve()
        self.legacy_root = Path(legacy_root).expanduser().resolve()
        self.ax_root = Path(ax_root).expanduser().resolve()
        if not self.target_checkout.is_dir():
            raise MigrationError(
                f"target checkout is not a directory: {self.target_checkout}"
            )
        if not self.legacy_root.is_dir():
            raise MigrationError(f"legacy overlay is not a directory: {self.legacy_root}")
        if _overlaps(self.ax_root, self.target_checkout):
            raise MigrationError("AX_ROOT must be disjoint from the target checkout")
        if _overlaps(self.ax_root, self.legacy_root):
            raise MigrationError("AX_ROOT must be disjoint from the legacy overlay")
        try:
            self.authority = AxPathAuthority(self.ax_source_root, self.ax_root)
            self.authority.assert_runtime_outside_target(self.target_checkout)
        except AxPathError as exc:
            raise MigrationError(str(exc)) from exc
        self.source_ref = _source_ref(self.target_checkout, source_ref)
        self.source_oid = _run_git(
            self.target_checkout, "rev-parse", f"{self.source_ref}^{{commit}}"
        )
        canonical_target_id = self.authority.canonical_target_identity(
            self.target_checkout
        )
        self.target_id = repo_id or canonical_target_id
        if not self.target_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in self.target_id
        ):
            raise MigrationError("repo_id must contain only letters, digits, '-' or '_'")
        seed = "\x00".join(
            (
                str(self.target_checkout),
                str(self.legacy_root),
                str(self.ax_root),
                self.source_ref,
                self.source_oid,
                self.target_id,
            )
        )
        self.migration_id = f"migration-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"

    @property
    def state_path(self) -> Path:
        return self.authority.state_root / "migrations" / f"{self.migration_id}.json"

    @property
    def pointer_path(self) -> Path:
        return self.authority.state_root / "active-control-plane.json"

    @property
    def cutover_backup_path(self) -> Path:
        return (
            self.authority.artifacts_root
            / "migrations"
            / self.migration_id
            / "cutover-before.json"
        )

    def plan(self, *, mode: str = "dry-run") -> MigrationPlan:
        if mode not in {"dry-run", "apply", "verify", "rollback", "cutover"}:
            raise MigrationError(f"unsupported migration mode: {mode}")
        mappings, archived = _read_registry(self.layout)
        files = _legacy_evidence_files(
            self.legacy_root, self.ax_root, self.migration_id
        )
        quarantined, preserved = _read_legacy_activations(
            self.legacy_root / "state" / "agent-team.db"
        )
        operations: list[dict[str, Any]] = []
        ordinal = 0
        for directory in (
            self.authority.repositories_root,
            self.authority.workspaces_root,
            self.authority.state_root,
            self.authority.artifacts_root,
            self.authority.activations_root,
        ):
            operations.append(
                {
                    "ordinal": ordinal,
                    "kind": "create",
                    "source": None,
                    "target": str(directory),
                    "source_sha256": None,
                    "reversible": True,
                }
            )
            ordinal += 1
        operations.append(
            {
                "ordinal": ordinal,
                "kind": "register",
                "source": str(self.target_checkout),
                "target": str(self.authority.managed_repository(self.target_id)),
                "source_sha256": None,
                "reversible": False,
            }
        )
        ordinal += 1
        for evidence in files:
            operations.append(
                {
                    "ordinal": ordinal,
                    "kind": "copy",
                    "source": evidence.source,
                    "target": evidence.target,
                    "source_sha256": evidence.sha256,
                    "reversible": False,
                }
            )
            ordinal += 1
        for activation_id in quarantined:
            operations.append(
                {
                    "ordinal": ordinal,
                    "kind": "quarantine",
                    "source": activation_id,
                    "target": "PL_REISSUE_REQUIRED",
                    "source_sha256": None,
                    "reversible": False,
                }
            )
            ordinal += 1
        operations.extend(
            [
                {
                    "ordinal": ordinal,
                    "kind": "verify",
                    "source": str(self.ax_root),
                    "target": str(self.authority.state_database),
                    "source_sha256": None,
                    "reversible": False,
                },
                {
                    "ordinal": ordinal + 1,
                    "kind": "cutover",
                    "source": str(self.pointer_path),
                    "target": str(self.ax_root),
                    "source_sha256": None,
                    "reversible": True,
                },
            ]
        )
        rollback_operations = [
            {
                "ordinal": 0,
                "kind": "cutover",
                "source": str(self.cutover_backup_path),
                "target": str(self.pointer_path),
                "source_sha256": None,
                "reversible": True,
            },
            {
                "ordinal": 1,
                "kind": "verify",
                "source": str(self.pointer_path),
                "target": str(self.ax_root),
                "source_sha256": None,
                "reversible": False,
            },
        ]
        state_ref = (
            str((self.legacy_root / "state" / "agent-team.db").resolve())
            if (self.legacy_root / "state" / "agent-team.db").is_file()
            else "absent"
        )
        manifest = _manifest_with_digest(
            {
                "schema_version": SCHEMA_VERSION,
                "migration_id": self.migration_id,
                "mode": mode,
                "source": {
                    "layout": "target-project-overlay",
                    "topology_version": 3,
                    "checkout_path": str(self.target_checkout),
                    "state_ref": state_ref,
                },
                "target": {
                    "layout": "independent-ax",
                    "topology_id": TOPOLOGY_ID,
                    "ax_root": str(self.ax_root),
                },
                "seat_mappings": mappings,
                "archived_seat_ids": archived,
                "quarantined_activation_ids": list(quarantined),
                "preserved_evidence_refs": [item.target for item in files],
                "operations": operations,
                "rollback_operations": rollback_operations,
                "created_at": DETERMINISTIC_CREATED_AT,
            }
        )
        deletion_entries = [
            {
                "ordinal": index,
                "relative_path": Path(item.source)
                .relative_to(self.legacy_root)
                .as_posix(),
                "ownership_digest": item.sha256,
                "reference_evidence_digest": item.sha256,
                "replacement_path": item.target,
                "disposition": "RETAIN",
            }
            for index, item in enumerate(files)
        ]
        deletion = _manifest_with_digest(
            {
                "schema_version": SCHEMA_VERSION,
                "migration_id": self.migration_id,
                "state": "DRY_RUN" if mode == "dry-run" else "APPROVED",
                "target_id": self.target_id,
                "entries": deletion_entries,
                "created_at": DETERMINISTIC_CREATED_AT,
            }
        )
        return MigrationPlan(
            migration_id=self.migration_id,
            target_id=self.target_id,
            source_ref=self.source_ref,
            source_oid=self.source_oid,
            target_checkout=self.target_checkout,
            legacy_root=self.legacy_root,
            ax_root=self.ax_root,
            evidence_files=files,
            quarantined_activation_ids=quarantined,
            preserved_activation_ids=preserved,
            migration_manifest=manifest,
            deletion_manifest=deletion,
        )

    def dry_run(self, *, output: Path | None = None) -> MigrationPlan:
        plan = self.plan(mode="dry-run")
        if output is not None:
            destination = _safe_output_path(
                Path(output),
                protected_roots=(
                    self.ax_source_root,
                    self.target_checkout,
                    self.legacy_root,
                    self.ax_root,
                ),
            )
            _atomic_write(destination, _canonical_json(plan.as_dict()))
        return plan

    def _copy_evidence(self, item: EvidenceFile) -> None:
        source = Path(item.source)
        target = self.authority.assert_runtime_path(item.target)
        if not source.is_file() or source.is_symlink():
            raise MigrationError(f"legacy evidence disappeared: {source}")
        if _sha256_file(source, preserve_access_time=True) != item.sha256:
            raise MigrationError(f"legacy evidence changed after planning: {source}")
        if target.exists():
            if not target.is_file() or _sha256_file(target) != item.sha256:
                raise MigrationError(f"preserved evidence path conflicts: {target}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.ax-copy-tmp")
        _copy_source_file(source, temporary)
        if _sha256_file(temporary) != item.sha256:
            temporary.unlink(missing_ok=True)
            raise MigrationError(f"evidence copy verification failed: {source}")
        try:
            os.chmod(temporary, stat.S_IREAD)
        except OSError:
            pass
        os.replace(temporary, target)

    def _record_v4_evidence(
        self,
        store: AxStateStore,
        plan: MigrationPlan,
        *,
        checkout_fingerprint: str,
        managed_repository: str,
    ) -> None:
        occurred_at = datetime.now(UTC).isoformat()
        manifest_digest = str(plan.migration_manifest["manifest_sha256"])
        deletion_digest = str(plan.deletion_manifest["manifest_sha256"])
        pointer_before = (
            _sha256_file(self.pointer_path) if self.pointer_path.is_file() else None
        )
        desired_pointer = self._desired_pointer(plan, Path(managed_repository))
        pointer_after = _sha256_bytes(_canonical_json(desired_pointer))
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO migration_runs (
                    id, target_id, legacy_root, runtime_root, state,
                    manifest_digest, recovery_snapshot_ref,
                    active_pointer_before, active_pointer_after,
                    idempotency_key, created_at, completed_at
                ) VALUES (?, ?, ?, ?, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    self.migration_id,
                    self.target_id,
                    str(self.legacy_root),
                    str(self.ax_root),
                    manifest_digest,
                    str(self.cutover_backup_path),
                    pointer_before,
                    pointer_after,
                    f"migration:{self.migration_id}",
                    occurred_at,
                    occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO deletion_manifests (
                    id, target_id, manifest_digest, state,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, 'VERIFIED', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    f"deletion-{self.migration_id}",
                    self.target_id,
                    deletion_digest,
                    f"deletion:{self.migration_id}",
                    occurred_at,
                ),
            )
            for entry in plan.deletion_manifest["entries"]:
                connection.execute(
                    """
                    INSERT INTO deletion_manifest_entries (
                        manifest_id, ordinal, relative_path, ownership_digest,
                        reference_evidence_digest, replacement_path, disposition
                    ) VALUES (?, ?, ?, ?, ?, ?, 'RETAIN')
                    ON CONFLICT(manifest_id, ordinal) DO NOTHING
                    """,
                    (
                        f"deletion-{self.migration_id}",
                        entry["ordinal"],
                        entry["relative_path"],
                        entry["ownership_digest"],
                        entry["reference_evidence_digest"],
                        entry["replacement_path"],
                    ),
                )
            activation_actions = [
                (item, "QUARANTINED")
                for item in plan.quarantined_activation_ids
            ] + [
                (item, "PRESERVED") for item in plan.preserved_activation_ids
            ]
            for activation_id, action in activation_actions:
                evidence_digest = _sha256_bytes(
                    _canonical_json(
                        {
                            "legacy_table": "activations",
                            "legacy_record_id": activation_id,
                            "action": action,
                            "checkout_fingerprint": checkout_fingerprint,
                        }
                    )
                )
                connection.execute(
                    """
                    INSERT INTO migration_evidence (
                        id, legacy_migration_id, legacy_table,
                        legacy_record_id, observed_state, action,
                        reissued_contract_id, evidence_digest,
                        idempotency_key, occurred_at
                    ) VALUES (?, ?, 'activations', ?, ?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(legacy_table, legacy_record_id, action) DO NOTHING
                    """,
                    (
                        f"migration-evidence-{hashlib.sha256((activation_id + action).encode()).hexdigest()[:24]}",
                        self.migration_id,
                        activation_id,
                        "AMBIGUOUS" if action == "QUARANTINED" else "TERMINAL",
                        action,
                        evidence_digest,
                        f"migration-evidence:{self.migration_id}:{activation_id}:{action}",
                        occurred_at,
                    ),
                )

    def _desired_pointer(
        self, plan: MigrationPlan, managed_repository: Path
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "migration_id": self.migration_id,
            "active_ax_root": str(self.ax_root),
            "state_database": str(self.authority.state_database),
            "target_id": self.target_id,
            "managed_repository": str(managed_repository),
            "source_oid": plan.source_oid,
            "topology_id": TOPOLOGY_ID,
            "max_threads": MAX_THREADS,
            "mcp_servers": REQUIRED_MCP,
            "migration_manifest_sha256": plan.migration_manifest[
                "manifest_sha256"
            ],
        }

    def apply(self) -> MigrationResult:
        plan = self.plan(mode="apply")
        before = _checkout_fingerprint(self.target_checkout)
        for directory in (
            self.authority.repositories_root,
            self.authority.workspaces_root,
            self.authority.state_root,
            self.authority.artifacts_root,
            self.authority.activations_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        store = AxStateStore(self.authority.state_database)
        service = ManagedRepositoryService(
            state_store=store,
            path_authority=self.authority,
        )
        registration = service.register_target(
            self.target_checkout,
            source_ref=self.source_ref,
            requested_target_id=self.target_id,
            idempotency_key=f"migration-register:{self.migration_id}",
        )
        if registration.observed_source_oid != plan.source_oid:
            raise MigrationError("target source ref changed during registration")
        snapshot = service.import_snapshot(
            registration.target_id,
            expected_source_oid=plan.source_oid,
            idempotency_key=f"migration-snapshot:{self.migration_id}",
        )
        if snapshot.imported_oid != plan.source_oid:
            raise MigrationError("managed repository imported an unexpected OID")
        for item in plan.evidence_files:
            self._copy_evidence(item)
        after = _checkout_fingerprint(self.target_checkout)
        if before != after:
            raise MigrationError(
                "target checkout changed during migration; evidence is retained for review"
            )
        self._record_v4_evidence(
            store,
            plan,
            checkout_fingerprint=before,
            managed_repository=registration.managed_repository_path,
        )
        state = {
            "schema_version": SCHEMA_VERSION,
            "migration_id": self.migration_id,
            "target_id": registration.target_id,
            "source_ref": self.source_ref,
            "source_oid": plan.source_oid,
            "managed_repository": registration.managed_repository_path,
            "checkout_fingerprint": before,
            "migration_manifest": dict(plan.migration_manifest),
            "deletion_manifest": dict(plan.deletion_manifest),
            "evidence_files": [asdict(item) for item in plan.evidence_files],
            "quarantined_activation_ids": list(
                plan.quarantined_activation_ids
            ),
            "preserved_activation_ids": list(plan.preserved_activation_ids),
        }
        if self.state_path.exists() and self.state_path.read_bytes() != _canonical_json(
            state
        ):
            raise MigrationError(f"migration state conflicts: {self.state_path}")
        _atomic_write(self.state_path, _canonical_json(state))
        durable_state = self._migration_run_state(store)
        cut_over = self._pointer_is_desired(state)
        rolled_back = durable_state == "ROLLED_BACK"
        if durable_state == "CUT_OVER" and not cut_over:
            raise MigrationError(
                "migration replay found CUT_OVER state with a divergent pointer"
            )
        if durable_state == "COMPLETED" and cut_over:
            raise MigrationError(
                "migration replay found an active pointer without CUT_OVER state"
            )
        if rolled_back:
            existed, prior = self._load_cutover_backup()
            if (
                not self._pointer_matches(existed, prior)
                or self._deletion_manifest_state(store) != "ROLLED_BACK"
            ):
                raise MigrationError(
                    "migration replay found inconsistent rollback state"
                )
        return self._result(
            operation="apply",
            state=state,
            verified=self._verify_state(state),
            cut_over=cut_over,
            rolled_back=rolled_back,
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            raise MigrationError(
                f"migration has not been applied: {self.state_path}"
            )
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"invalid migration state: {self.state_path}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("migration_id") != self.migration_id
            or value.get("target_id") != self.target_id
        ):
            raise MigrationError("migration state identity does not match request")
        return value

    def _verify_state(self, state: Mapping[str, Any]) -> bool:
        manifest = state.get("migration_manifest")
        if not isinstance(manifest, dict):
            raise MigrationError("migration manifest is missing")
        expected_digest = manifest.get("manifest_sha256")
        if _manifest_with_digest(manifest).get("manifest_sha256") != expected_digest:
            raise MigrationError("migration manifest digest is invalid")
        deletion = state.get("deletion_manifest")
        if not isinstance(deletion, dict):
            raise MigrationError("deletion manifest is missing")
        if _manifest_with_digest(deletion).get("manifest_sha256") != deletion.get(
            "manifest_sha256"
        ):
            raise MigrationError("deletion manifest digest is invalid")
        for raw in state.get("evidence_files", []):
            item = EvidenceFile(**raw)
            target = self.authority.assert_runtime_path(item.target)
            if not target.is_file() or _sha256_file(target) != item.sha256:
                raise MigrationError(f"preserved evidence verification failed: {target}")
        database = self.authority.state_database
        if not database.is_file():
            raise MigrationError(f"v4 state database is missing: {database}")
        store = AxStateStore(database)
        if store.schema_version() != SCHEMA_VERSION:
            raise MigrationError("AX state database is not schema v4")
        managed = Path(str(state.get("managed_repository", ""))).resolve()
        expected_managed = self.authority.managed_repository(self.target_id)
        if managed != expected_managed or not (managed / "HEAD").is_file():
            raise MigrationError("managed bare repository is missing or misplaced")
        service = ManagedRepositoryService(
            state_store=store,
            path_authority=self.authority,
        )
        if service.resolve_commit(self.target_id, str(state["source_oid"])) != state[
            "source_oid"
        ]:
            raise MigrationError("managed source snapshot is not resolvable")
        return True

    def verify(self) -> MigrationResult:
        state = self._load_state()
        store = AxStateStore(self.authority.state_database)
        durable_state = self._migration_run_state(store)
        cut_over = self._pointer_is_desired(state)
        rolled_back = durable_state == "ROLLED_BACK"
        if durable_state == "CUT_OVER" and not cut_over:
            raise MigrationError(
                "migration state is CUT_OVER but the control pointer differs"
            )
        if durable_state == "COMPLETED" and cut_over:
            raise MigrationError(
                "control pointer is active while migration state is only COMPLETED"
            )
        if durable_state not in {"COMPLETED", "CUT_OVER", "ROLLED_BACK"}:
            raise MigrationError(
                f"migration has an incomplete durable operation: {durable_state}"
            )
        if rolled_back:
            existed, prior = self._load_cutover_backup()
            if not self._pointer_matches(existed, prior):
                raise MigrationError(
                    "migration state is ROLLED_BACK but the prior pointer was not restored"
                )
            if self._deletion_manifest_state(store) != "ROLLED_BACK":
                raise MigrationError(
                    "rolled-back migration and deletion manifest states differ"
                )
        return self._result(
            operation="verify",
            state=state,
            verified=self._verify_state(state),
            cut_over=cut_over,
            rolled_back=rolled_back,
        )

    def _pointer_is_desired(self, state: Mapping[str, Any]) -> bool:
        if not self.pointer_path.is_file():
            return False
        plan = self.plan(mode="apply")
        desired = self._desired_pointer(
            plan, Path(str(state["managed_repository"]))
        )
        return self.pointer_path.read_bytes() == _canonical_json(desired)

    def _migration_run_state(self, store: AxStateStore) -> str:
        with store.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM migration_runs WHERE id = ?",
                (self.migration_id,),
            ).fetchone()
        if row is None:
            raise MigrationError("durable migration run is missing")
        return str(row["state"])

    def _deletion_manifest_state(self, store: AxStateStore) -> str:
        with store.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM deletion_manifests WHERE id = ?",
                (f"deletion-{self.migration_id}",),
            ).fetchone()
        if row is None:
            raise MigrationError("durable deletion manifest is missing")
        return str(row["state"])

    def _capture_pointer(self) -> tuple[bool, bytes]:
        if not self.pointer_path.exists():
            return False, b""
        if not self.pointer_path.is_file() or self.pointer_path.is_symlink():
            raise MigrationError("control pointer is not a regular file")
        return True, self.pointer_path.read_bytes()

    def _pointer_matches(self, existed: bool, content: bytes) -> bool:
        if not existed:
            return not self.pointer_path.exists()
        return (
            self.pointer_path.is_file()
            and not self.pointer_path.is_symlink()
            and self.pointer_path.read_bytes() == content
        )

    def _restore_pointer(self, existed: bool, content: bytes) -> None:
        if self._pointer_matches(existed, content):
            return
        if existed:
            _atomic_write(self.pointer_path, content)
        elif self.pointer_path.exists():
            if not self.pointer_path.is_file() or self.pointer_path.is_symlink():
                raise MigrationError(
                    "refusing to remove an unexpected control pointer path"
                )
            self.pointer_path.unlink()
        if not self._pointer_matches(existed, content):
            raise MigrationError("control pointer compensation verification failed")

    def _load_cutover_backup(self) -> tuple[bool, bytes]:
        if not self.cutover_backup_path.is_file():
            raise MigrationError("cutover backup does not exist")
        try:
            backup = json.loads(
                self.cutover_backup_path.read_text(encoding="utf-8")
            )
            prior = base64.b64decode(backup["content_base64"], validate=True)
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError("cutover backup is invalid") from exc
        if (
            backup.get("schema_version") != SCHEMA_VERSION
            or backup.get("migration_id") != self.migration_id
            or backup.get("pointer_path") != str(self.pointer_path)
            or not isinstance(backup.get("existed"), bool)
            or _sha256_bytes(prior) != backup.get("sha256")
        ):
            raise MigrationError("cutover backup digest or identity is invalid")
        return bool(backup["existed"]), prior

    def _ensure_cutover_backup(self, existed: bool, prior: bytes) -> None:
        if not self.cutover_backup_path.exists():
            backup = {
                "schema_version": SCHEMA_VERSION,
                "migration_id": self.migration_id,
                "pointer_path": str(self.pointer_path),
                "existed": existed,
                "content_base64": base64.b64encode(prior).decode("ascii"),
                "sha256": _sha256_bytes(prior),
            }
            _atomic_write(
                self.cutover_backup_path,
                _canonical_json(backup),
                read_only=True,
            )
        backed_up_existed, backed_up_prior = self._load_cutover_backup()
        if (backed_up_existed, backed_up_prior) != (existed, prior):
            raise MigrationError(
                "cutover backup does not match the current pointer generation"
            )

    def _mark_cut_over(self, store: AxStateStore, occurred_at: str) -> None:
        with store.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE migration_runs
                SET state = 'CUT_OVER', completed_at = ?
                WHERE id = ? AND state = 'RUNNING'
                """,
                (occurred_at, self.migration_id),
            )
            if cursor.rowcount != 1:
                raise MigrationError(
                    "cutover durable-state compare-and-swap did not update one row"
                )

    def _reserve_cutover(self, store: AxStateStore) -> None:
        with store.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE migration_runs SET state = 'RUNNING'
                WHERE id = ? AND state = 'COMPLETED'
                """,
                (self.migration_id,),
            )
            if cursor.rowcount != 1:
                raise MigrationError(
                    "cutover reservation compare-and-swap did not update one row"
                )

    def _release_cutover_reservation(self, store: AxStateStore) -> None:
        with store.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE migration_runs SET state = 'COMPLETED'
                WHERE id = ? AND state = 'RUNNING'
                """,
                (self.migration_id,),
            )
            if cursor.rowcount != 1:
                raise MigrationError(
                    "cutover compensation compare-and-swap did not update one row"
                )

    def _compensate_cutover(
        self,
        store: AxStateStore,
        *,
        pointer_existed: bool,
        prior_pointer: bytes,
    ) -> None:
        self._restore_pointer(pointer_existed, prior_pointer)
        self._release_cutover_reservation(store)
        if (
            self._migration_run_state(store) != "COMPLETED"
            or not self._pointer_matches(pointer_existed, prior_pointer)
        ):
            raise MigrationError("cutover compensation postcondition failed")

    def _mark_rolled_back(
        self,
        store: AxStateStore,
        *,
        occurred_at: str,
        evidence_digest: str,
    ) -> None:
        with store.transaction(immediate=True) as connection:
            migration = connection.execute(
                """
                UPDATE migration_runs
                SET state = 'ROLLED_BACK', completed_at = ?
                WHERE id = ? AND state = 'ROLLING_BACK'
                """,
                (occurred_at, self.migration_id),
            )
            if migration.rowcount != 1:
                raise MigrationError(
                    "rollback durable-state compare-and-swap did not update one row"
                )
            deletion = connection.execute(
                """
                UPDATE deletion_manifests SET state = 'ROLLED_BACK'
                WHERE id = ? AND state = 'VERIFIED'
                """,
                (f"deletion-{self.migration_id}",),
            )
            if deletion.rowcount != 1:
                raise MigrationError(
                    "rollback deletion-manifest compare-and-swap did not update one row"
                )
            connection.execute(
                """
                INSERT INTO migration_evidence (
                    id, legacy_migration_id, legacy_table, legacy_record_id,
                    observed_state, action, reissued_contract_id,
                    evidence_digest, idempotency_key, occurred_at
                ) VALUES (?, ?, 'control_pointer', ?, 'CUT_OVER',
                          'ROLLED_BACK', NULL, ?, ?, ?)
                ON CONFLICT(legacy_table, legacy_record_id, action) DO NOTHING
                """,
                (
                    f"migration-rollback-{self.migration_id}",
                    self.migration_id,
                    self.migration_id,
                    evidence_digest,
                    f"migration-rollback:{self.migration_id}",
                    occurred_at,
                ),
            )

    def _reserve_rollback(self, store: AxStateStore) -> None:
        with store.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE migration_runs SET state = 'ROLLING_BACK'
                WHERE id = ? AND state = 'CUT_OVER'
                """,
                (self.migration_id,),
            )
            if cursor.rowcount != 1:
                raise MigrationError(
                    "rollback reservation compare-and-swap did not update one row"
                )

    def _release_rollback_reservation(self, store: AxStateStore) -> None:
        with store.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE migration_runs SET state = 'CUT_OVER'
                WHERE id = ? AND state = 'ROLLING_BACK'
                """,
                (self.migration_id,),
            )
            if cursor.rowcount != 1:
                raise MigrationError(
                    "rollback compensation compare-and-swap did not update one row"
                )

    def _compensate_rollback(
        self,
        store: AxStateStore,
        *,
        desired_pointer: bytes,
    ) -> None:
        self._restore_pointer(True, desired_pointer)
        self._release_rollback_reservation(store)
        if (
            self._migration_run_state(store) != "CUT_OVER"
            or not self._pointer_matches(True, desired_pointer)
        ):
            raise MigrationError("rollback compensation postcondition failed")

    def cutover(self) -> MigrationResult:
        state = self._load_state()
        self._verify_state(state)
        plan = self.plan(mode="apply")
        desired = self._desired_pointer(
            plan, Path(str(state["managed_repository"]))
        )
        store = AxStateStore(self.authority.state_database)
        durable_state = self._migration_run_state(store)
        desired_bytes = _canonical_json(desired)
        if durable_state == "CUT_OVER":
            if not self._pointer_matches(True, desired_bytes):
                raise MigrationError(
                    "repeated cutover found a divergent control pointer"
                )
            return self._result(
                operation="cutover",
                state=state,
                verified=True,
                cut_over=True,
                rolled_back=False,
            )
        if durable_state == "ROLLED_BACK":
            raise MigrationError(
                "re-cutover after rollback requires a new migration generation"
            )
        if durable_state != "COMPLETED":
            raise MigrationError(
                f"cutover requires COMPLETED state, found {durable_state}"
            )
        existed, prior = self._capture_pointer()
        self._ensure_cutover_backup(existed, prior)
        self._reserve_cutover(store)
        if not self._pointer_matches(existed, prior):
            self._release_cutover_reservation(store)
            raise MigrationError(
                "control pointer changed before the reserved cutover swap"
            )
        try:
            _atomic_write(self.pointer_path, desired_bytes)
            if not self._pointer_matches(True, desired_bytes):
                raise MigrationError("control pointer swap verification failed")
        except Exception as exc:
            try:
                self._compensate_cutover(
                    store,
                    pointer_existed=existed,
                    prior_pointer=prior,
                )
            except Exception as compensation_error:
                raise MigrationError(
                    "cutover pointer swap failed and compensation also failed"
                ) from compensation_error
            raise MigrationError(
                "cutover pointer swap failed; prior pointer was restored"
            ) from exc
        try:
            self._mark_cut_over(store, datetime.now(UTC).isoformat())
        except Exception as exc:
            try:
                self._compensate_cutover(
                    store,
                    pointer_existed=existed,
                    prior_pointer=prior,
                )
            except Exception as compensation_error:
                raise MigrationError(
                    "cutover state update failed and pointer compensation failed"
                ) from compensation_error
            raise MigrationError(
                "cutover state update failed; prior pointer was restored"
            ) from exc
        if (
            self._migration_run_state(store) != "CUT_OVER"
            or not self._pointer_matches(True, desired_bytes)
        ):
            raise MigrationError("cutover postcondition verification failed")
        return self._result(
            operation="cutover",
            state=state,
            verified=True,
            cut_over=True,
            rolled_back=False,
        )

    def rollback(self) -> MigrationResult:
        state = self._load_state()
        self._verify_state(state)
        store = AxStateStore(self.authority.state_database)
        durable_state = self._migration_run_state(store)
        existed, prior = self._load_cutover_backup()
        if durable_state == "ROLLED_BACK":
            if not self._pointer_matches(existed, prior):
                raise MigrationError(
                    "repeated rollback found a divergent control pointer"
                )
            if self._deletion_manifest_state(store) != "ROLLED_BACK":
                raise MigrationError(
                    "rollback state and deletion manifest state diverged"
                )
            return self._result(
                operation="rollback",
                state=state,
                verified=True,
                cut_over=False,
                rolled_back=True,
            )
        if durable_state != "CUT_OVER":
            raise MigrationError(
                f"rollback requires CUT_OVER state, found {durable_state}"
            )
        plan = self.plan(mode="apply")
        desired = self._desired_pointer(
            plan, Path(str(state["managed_repository"]))
        )
        desired_bytes = _canonical_json(desired)
        if not self._pointer_matches(True, desired_bytes):
            raise MigrationError(
                "rollback refused because the active control pointer diverged"
            )
        self._reserve_rollback(store)
        if not self._pointer_matches(True, desired_bytes):
            self._release_rollback_reservation(store)
            raise MigrationError(
                "control pointer changed before the reserved rollback restore"
            )
        try:
            self._restore_pointer(existed, prior)
        except Exception as exc:
            try:
                self._compensate_rollback(
                    store, desired_pointer=desired_bytes
                )
            except Exception as compensation_error:
                raise MigrationError(
                    "rollback pointer restore failed and compensation also failed"
                ) from compensation_error
            raise MigrationError(
                "rollback pointer restore failed; active pointer was preserved"
            ) from exc
        occurred_at = datetime.now(UTC).isoformat()
        evidence_digest = _sha256_bytes(
            _canonical_json(
                {
                    "migration_id": self.migration_id,
                    "pointer_path": str(self.pointer_path),
                    "existed": existed,
                    "content_sha256": _sha256_bytes(prior),
                }
            )
        )
        try:
            self._mark_rolled_back(
                store,
                occurred_at=occurred_at,
                evidence_digest=evidence_digest,
            )
        except Exception as exc:
            try:
                self._compensate_rollback(
                    store, desired_pointer=desired_bytes
                )
            except Exception as compensation_error:
                raise MigrationError(
                    "rollback state update failed and pointer compensation failed"
                ) from compensation_error
            raise MigrationError(
                "rollback state update failed; active pointer was restored"
            ) from exc
        if (
            self._migration_run_state(store) != "ROLLED_BACK"
            or self._deletion_manifest_state(store) != "ROLLED_BACK"
            or not self._pointer_matches(existed, prior)
        ):
            raise MigrationError("rollback postcondition verification failed")
        return self._result(
            operation="rollback",
            state=state,
            verified=True,
            cut_over=False,
            rolled_back=True,
        )

    def _result(
        self,
        *,
        operation: str,
        state: Mapping[str, Any],
        verified: bool,
        cut_over: bool,
        rolled_back: bool,
    ) -> MigrationResult:
        return MigrationResult(
            operation=operation,
            migration_id=self.migration_id,
            target_id=self.target_id,
            ax_root=self.ax_root,
            managed_repository=Path(str(state["managed_repository"])),
            source_oid=str(state["source_oid"]),
            evidence_file_count=len(state.get("evidence_files", [])),
            quarantined_activation_count=len(
                state.get("quarantined_activation_ids", [])
            ),
            pointer_path=self.pointer_path,
            verified=verified,
            cut_over=cut_over,
            rolled_back=rolled_back,
        )


def plan_migration(**kwargs: Any) -> MigrationPlan:
    """Public in-memory dry-run API; no AX_ROOT or target writes occur."""

    return AgentTeamMigrator(**kwargs).plan(mode="dry-run")


def dry_run_migration(*, output: Path | None = None, **kwargs: Any) -> MigrationPlan:
    return AgentTeamMigrator(**kwargs).dry_run(output=output)


def apply_migration(**kwargs: Any) -> MigrationResult:
    return AgentTeamMigrator(**kwargs).apply()


def verify_migration(**kwargs: Any) -> MigrationResult:
    return AgentTeamMigrator(**kwargs).verify()


def cutover_migration(**kwargs: Any) -> MigrationResult:
    return AgentTeamMigrator(**kwargs).cutover()


def rollback_migration(**kwargs: Any) -> MigrationResult:
    return AgentTeamMigrator(**kwargs).rollback()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate a target-local v3 overlay into an independent AX_ROOT."
    )
    parser.add_argument(
        "command", choices=("dry-run", "apply", "verify", "cutover", "rollback")
    )
    parser.add_argument("--ax-source-root", type=Path, default=Path.cwd())
    parser.add_argument("--target-checkout", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--ax-root", type=Path, required=True)
    parser.add_argument("--source-ref")
    parser.add_argument("--repo-id")
    parser.add_argument(
        "--output",
        type=Path,
        help="Safe output file for dry-run manifests; omitted means in-memory/stdout only.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = args.target_checkout.expanduser().resolve()
    legacy = args.legacy_root or (target / ".agent-team")
    try:
        migrator = AgentTeamMigrator(
            ax_source_root=args.ax_source_root,
            target_checkout=target,
            legacy_root=legacy,
            ax_root=args.ax_root,
            source_ref=args.source_ref,
            repo_id=args.repo_id,
        )
        if args.command == "dry-run":
            result: MigrationPlan | MigrationResult = migrator.dry_run(
                output=args.output
            )
        else:
            if args.output is not None:
                raise MigrationError("--output is valid only with dry-run")
            operations = {
                "apply": migrator.apply,
                "verify": migrator.verify,
                "cutover": migrator.cutover,
                "rollback": migrator.rollback,
            }
            result = operations[args.command]()
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except (MigrationError, AxPathError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Agent-Team migration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
