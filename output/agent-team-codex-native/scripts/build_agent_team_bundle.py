#!/usr/bin/env python3
"""Materialize and verify the Agent-Team delivery bundle deterministically."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

try:
    from scripts.agent_team_layout import (
        AgentTeamLayout,
        AgentTeamLayoutError,
    )
    from scripts.project_agents import compile_runtime_agent, load_and_validate
except ModuleNotFoundError:
    from agent_team_layout import AgentTeamLayout, AgentTeamLayoutError
    from project_agents import compile_runtime_agent, load_and_validate


BUNDLE_ID = "agent-team-codex-native"
BUNDLE_FORMAT_VERSION = 4
GENERATED_MANIFEST = "bundle-build-manifest.json"
LEGACY_MANIFEST = "bundle-manifest.toml"
IGNORED_NAMES = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
PROJECT_AGENTS_PATH = "scripts/project_agents.py"


class BundleBuildError(RuntimeError):
    """Raised when deterministic bundle materialization would be unsafe."""


@dataclass(frozen=True)
class BundleBuildReport:
    destination: Path
    created: tuple[str, ...]
    updated: tuple[str, ...]
    removed_generated: tuple[str, ...]
    preserved_unknown: tuple[str, ...]
    manifest_sha256: str

    @property
    def has_drift(self) -> bool:
        return bool(self.created or self.updated or self.removed_generated)

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["destination"] = str(self.destination)
        value["has_drift"] = self.has_drift
        return value


@dataclass(frozen=True)
class _BundleFile:
    path: str
    content: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _relative_text(path: PurePosixPath | Path) -> str:
    return PurePosixPath(*path.parts).as_posix()


def _source_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return (
        path
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
        and not any(part in IGNORED_NAMES for part in path.parts)
        and path.suffix.lower() not in IGNORED_SUFFIXES
    )


def _mapped_source_files(layout: AgentTeamLayout) -> dict[str, _BundleFile]:
    mappings = (
        (layout.scripts_root, PurePosixPath("scripts")),
        (layout.config_root, PurePosixPath("config/agent-team")),
        (layout.skill_root, PurePosixPath(".agents/skills")),
        (layout.profile_root, PurePosixPath("profile")),
    )
    result: dict[str, _BundleFile] = {}
    for source_root, destination_root in mappings:
        for source in _source_files(source_root):
            relative = PurePosixPath(*source.relative_to(source_root).parts)
            destination = (destination_root / relative).as_posix()
            if destination in result:
                raise BundleBuildError(f"Duplicate generated bundle path: {destination}")
            mode = stat.S_IMODE(source.stat().st_mode)
            content = source.read_bytes()
            if destination == PROJECT_AGENTS_PATH:
                content = _bundle_project_agents(content)
            result[destination] = _BundleFile(
                path=destination,
                content=content,
                mode=mode,
            )
    sample_config = layout.source_root / "sample_config.toml"
    if not sample_config.is_file():
        raise BundleBuildError(f"Required bundle source is missing: {sample_config}")
    result["sample_config.toml"] = _BundleFile(
        path="sample_config.toml",
        content=sample_config.read_bytes(),
        mode=stat.S_IMODE(sample_config.stat().st_mode),
    )
    return result


def _bundle_project_agents(content: bytes) -> bytes:
    """Adapt canonical logical paths to their physical bundle roots.

    Canonical configuration intentionally keeps `agents/...` and `skills/...`
    references digest-stable.  A bundle stores those trees under
    `config/agent-team` and `.agents/skills`, so only this generated copy uses
    the layout resolver.  The repository source remains authoritative.
    """

    text = content.decode("utf-8")
    source = "    return _inside_project(PROJECT_ROOT.joinpath(*logical.parts))\n"
    replacement = (
        "    if logical.parts[:2] == (\".codex\", \"agents\"):\n"
        "        return _inside_project(PROJECT_ROOT.joinpath(*logical.parts))\n"
        "    return LAYOUT.resolve_source_path(logical)\n"
    )
    if text.count(source) != 1:
        raise BundleBuildError(
            "project_agents.py bundle path adapter source marker changed"
        )
    return text.replace(source, replacement).encode("utf-8")


def _compiled_runtime_agents() -> dict[str, _BundleFile]:
    bundle = load_and_validate()
    result: dict[str, _BundleFile] = {}
    for seat_id in sorted(bundle["seats"]):
        path = f".codex/agents/{seat_id}.toml"
        result[path] = _BundleFile(
            path=path,
            content=compile_runtime_agent(bundle, seat_id).encode("utf-8"),
            mode=0o644,
        )
    return result


def _desired_files(layout: AgentTeamLayout) -> dict[str, _BundleFile]:
    files = _mapped_source_files(layout)
    for path, item in _compiled_runtime_agents().items():
        if path in files:
            raise BundleBuildError(f"Duplicate generated bundle path: {path}")
        files[path] = item
    return dict(sorted(files.items()))


def _manifest_bytes(files: Mapping[str, _BundleFile]) -> bytes:
    inventory = [
        {
            "path": path,
            "sha256": item.sha256,
            "size": len(item.content),
            "mode": f"{item.mode:04o}",
        }
        for path, item in sorted(files.items())
    ]
    payload = {
        "bundle_id": BUNDLE_ID,
        "format_version": BUNDLE_FORMAT_VERSION,
        "source_contract": {
            "schema_version": 4,
            "topology_id": "six-slot-v1",
            "fixed_seats": 5,
            "elastic_slots": 1,
            "max_threads": 6,
            "workflow": "delivery-v4@4.0.0",
            "migration_controller": "scripts/agent_team_migration.py",
            "required_mcp_servers": ["serena", "sequentialthinking"],
        },
        "generated_paths": [entry["path"] for entry in inventory],
        "inventory": inventory,
    }
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _legacy_identity(destination: Path) -> bool:
    manifest = destination / LEGACY_MANIFEST
    if not manifest.is_file():
        return False
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return f'id = "{BUNDLE_ID}"' in text


def _load_previous_manifest(destination: Path) -> dict[str, object] | None:
    manifest = destination / GENERATED_MANIFEST
    if not manifest.is_file():
        return None
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleBuildError(f"Invalid generated bundle manifest: {manifest}") from exc
    if (
        not isinstance(value, dict)
        or value.get("bundle_id") != BUNDLE_ID
        or value.get("format_version") not in {3, BUNDLE_FORMAT_VERSION}
        or not isinstance(value.get("generated_paths"), list)
        or not all(isinstance(path, str) and path for path in value["generated_paths"])
    ):
        raise BundleBuildError(f"Unexpected generated bundle identity: {manifest}")
    return value


def _validate_destination(layout: AgentTeamLayout, destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    source_root = layout.source_root.resolve()
    release_destination = layout.default_bundle_root.resolve()
    if destination == source_root or _is_relative_to(source_root, destination):
        raise BundleBuildError(
            f"Bundle destination cannot contain the canonical source root: {destination}"
        )
    if _is_relative_to(destination, source_root) and destination != release_destination:
        raise BundleBuildError(
            "Bundle destination inside the source checkout is restricted to the "
            f"release artifact path: {release_destination}"
        )
    for protected in (
        source_root / ".git",
        layout.runtime_state_root,
        source_root / ".codex",
    ):
        if destination == protected or _is_relative_to(destination, protected):
            raise BundleBuildError(f"Bundle destination is protected: {destination}")
    if destination.is_symlink():
        raise BundleBuildError(f"Bundle destination cannot be a symlink: {destination}")
    if destination.exists() and not destination.is_dir():
        raise BundleBuildError(f"Bundle destination is not a directory: {destination}")
    if destination.is_dir() and any(destination.iterdir()):
        previous = _load_previous_manifest(destination)
        if previous is None and not _legacy_identity(destination):
            raise BundleBuildError(
                "Refusing to materialize into a non-empty directory without the "
                f"{BUNDLE_ID!r} bundle identity: {destination}"
            )
    return destination


def _existing_files(destination: Path) -> set[str]:
    if not destination.is_dir():
        return set()
    return {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }


def _remove_empty_parents(path: Path, destination: Path) -> None:
    parent = path.parent
    while parent != destination:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _write_file(destination: Path, item: _BundleFile) -> None:
    target = destination.joinpath(*PurePosixPath(item.path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.agent-team-tmp")
    temporary.write_bytes(item.content)
    try:
        os.chmod(temporary, item.mode)
    except OSError:
        pass
    temporary.replace(target)


def build_bundle(
    source_root: Path,
    destination: Path,
    *,
    check: bool,
) -> BundleBuildReport:
    layout = AgentTeamLayout.discover(source_root)
    if layout.bundle_mode:
        raise BundleBuildError("A generated bundle cannot be used as canonical source.")
    destination = _validate_destination(layout, Path(destination))
    desired = _desired_files(layout)
    manifest_content = _manifest_bytes(desired)
    manifest_sha256 = hashlib.sha256(manifest_content).hexdigest()
    desired_with_manifest = {
        **desired,
        GENERATED_MANIFEST: _BundleFile(
            path=GENERATED_MANIFEST,
            content=manifest_content,
            mode=0o644,
        ),
    }

    previous = _load_previous_manifest(destination) if destination.is_dir() else None
    previous_generated = (
        set(previous["generated_paths"]) | {GENERATED_MANIFEST}
        if previous is not None
        else set()
    )
    existing = _existing_files(destination)
    created: list[str] = []
    updated: list[str] = []
    for path, item in sorted(desired_with_manifest.items()):
        target = destination.joinpath(*PurePosixPath(path).parts)
        if not target.is_file():
            created.append(path)
        elif target.read_bytes() != item.content:
            updated.append(path)
    removed = sorted(previous_generated - set(desired_with_manifest))
    preserved = sorted(existing - previous_generated - set(desired_with_manifest))

    if not check:
        destination.mkdir(parents=True, exist_ok=True)
        for path in removed:
            target = destination.joinpath(*PurePosixPath(path).parts)
            if target.is_file():
                target.unlink()
                _remove_empty_parents(target, destination)
        for path in (*created, *updated):
            _write_file(destination, desired_with_manifest[path])

    return BundleBuildReport(
        destination=destination,
        created=tuple(created),
        updated=tuple(updated),
        removed_generated=tuple(removed),
        preserved_unknown=tuple(preserved),
        manifest_sha256=manifest_sha256,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize or check the deterministic Agent-Team bundle."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--materialize", action="store_true")
    action.add_argument("--check", action="store_true")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=AgentTeamLayout.discover().source_root,
        help="Canonical repository root. Defaults to the discovered source root.",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        help="Bundle destination. Defaults to output/agent-team-codex-native.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        layout = AgentTeamLayout.discover(args.source_root)
        destination = args.destination or layout.default_bundle_root
        report = build_bundle(
            args.source_root,
            destination,
            check=bool(args.check),
        )
    except (AgentTeamLayoutError, BundleBuildError, OSError, ValueError) as exc:
        print(f"Agent-Team bundle error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 1 if args.check and report.has_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
