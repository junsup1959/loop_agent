#!/usr/bin/env python3
"""Discover canonical Agent-Team source and generated-bundle layouts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class AgentTeamLayoutError(ValueError):
    """Raised when an Agent-Team path is missing, ambiguous, or unsafe."""


def _relative_path(value: str | Path) -> PurePosixPath:
    raw = str(value).replace("\\", "/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (relative.parts and ":" in relative.parts[0])
    ):
        raise AgentTeamLayoutError(
            f"Path must be normalized and relative to the Agent-Team root: {value!r}"
        )
    return relative


@dataclass(frozen=True)
class AgentTeamLayout:
    """Resolved paths for canonical source or a materialized delivery bundle."""

    source_root: Path
    config_root: Path
    skill_root: Path
    profile_root: Path
    runtime_agent_root: Path
    bundle_mode: bool

    @classmethod
    def discover(cls, anchor: Path | None = None) -> "AgentTeamLayout":
        start = Path(anchor or __file__).expanduser().resolve()
        if start.is_file():
            start = start.parent
        candidates = (start, *start.parents)
        for candidate in candidates:
            source_markers = (
                candidate / "agents" / "team.toml",
                candidate / "skills" / "catalog.toml",
            )
            bundle_markers = (
                candidate / "config" / "agent-team" / "team.toml",
                candidate / ".agents" / "skills" / "catalog.toml",
            )
            if all(path.is_file() for path in source_markers):
                return cls(
                    source_root=candidate,
                    config_root=candidate / "agents",
                    skill_root=candidate / "skills",
                    profile_root=candidate / "profile",
                    runtime_agent_root=candidate / ".codex" / "agents",
                    bundle_mode=False,
                )
            if all(path.is_file() for path in bundle_markers):
                return cls(
                    source_root=candidate,
                    config_root=candidate / "config" / "agent-team",
                    skill_root=candidate / ".agents" / "skills",
                    profile_root=candidate / "profile",
                    runtime_agent_root=candidate / ".codex" / "agents",
                    bundle_mode=True,
                )
        raise AgentTeamLayoutError(
            f"Cannot locate an Agent-Team source or bundle layout from: {start}"
        )

    @property
    def scripts_root(self) -> Path:
        return self.source_root / "scripts"

    @property
    def skill_catalog_path(self) -> Path:
        return self.skill_root / "catalog.toml"

    @property
    def team_path(self) -> Path:
        return self.config_root / "team.toml"

    @property
    def default_bundle_root(self) -> Path:
        return self.source_root / "output" / "agent-team-codex-native"

    @property
    def runtime_state_root(self) -> Path:
        return self.source_root / ".agent-team"

    @property
    def frozen_legacy_skill_root(self) -> Path:
        return self.source_root / ".codex" / "skills"

    def resolve_source_path(self, relative: str | Path) -> Path:
        """Resolve a canonical logical path in either physical layout."""

        logical = _relative_path(relative)
        head, *tail = logical.parts
        roots = {
            "agents": self.config_root,
            "skills": self.skill_root,
            "profile": self.profile_root,
            "scripts": self.scripts_root,
        }
        try:
            root = roots[head]
        except KeyError as exc:
            raise AgentTeamLayoutError(
                "Canonical source paths must start with agents/, skills/, "
                f"profile/, or scripts/: {relative!r}"
            ) from exc
        return self.require_within_source(root.joinpath(*tail))

    def require_within_source(self, path: Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        try:
            relative = resolved.relative_to(self.source_root)
        except ValueError as exc:
            raise AgentTeamLayoutError(
                f"Path escapes the Agent-Team root: {resolved}"
            ) from exc
        if relative.parts[:1] in {(".git",), (".agent-team",)}:
            raise AgentTeamLayoutError(
                f"Runtime or Git state is not canonical source: {resolved}"
            )
        try:
            resolved.relative_to(self.frozen_legacy_skill_root)
        except ValueError:
            pass
        else:
            raise AgentTeamLayoutError(
                f"Frozen .codex/skills content is not canonical source: {resolved}"
            )
        return resolved

    def classify(self, path: Path) -> str:
        resolved = Path(path).expanduser().resolve()
        classifications = (
            ("runtime-state", self.runtime_state_root),
            ("legacy-skills", self.frozen_legacy_skill_root),
            ("runtime-agents", self.runtime_agent_root),
            ("config", self.config_root),
            ("skills", self.skill_root),
            ("profile", self.profile_root),
            ("scripts", self.scripts_root),
        )
        for label, root in classifications:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return label
        try:
            resolved.relative_to(self.source_root)
        except ValueError:
            return "external"
        return "other"

    def logical_path(self, path: Path) -> PurePosixPath:
        """Return the canonical root-source path for a physical source file."""

        resolved = self.require_within_source(path)
        mappings = (
            ("agents", self.config_root),
            ("skills", self.skill_root),
            ("profile", self.profile_root),
            ("scripts", self.scripts_root),
        )
        for prefix, root in mappings:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            return PurePosixPath(prefix, *relative.parts)
        raise AgentTeamLayoutError(f"Path is not canonical Agent-Team source: {resolved}")

    def relative_to_root(self, path: Path) -> PurePosixPath:
        resolved = self.require_within_source(path)
        return PurePosixPath(*resolved.relative_to(self.source_root).parts)
