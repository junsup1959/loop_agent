from __future__ import annotations

"""Disjoint path authorities for the Agent-Team AX runtime subsystem."""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

try:
    from .agent_team_domain import require_identifier
    from .agent_team_layout import AgentTeamLayout
except ImportError:  # Direct script/module execution from the repository root.
    from agent_team_domain import require_identifier
    from agent_team_layout import AgentTeamLayout


class AxPathError(ValueError):
    """Raised when an AX path overlaps, escapes, or cannot be translated safely."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(left: Path, right: Path) -> bool:
    return _contains(left, right) or _contains(right, left)


def _canonical_path_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    if os.name == "nt":
        normalized = normalized.casefold()
    return normalized.replace("\\", "/").rstrip("/")


@dataclass(frozen=True, slots=True)
class AxPathAuthority:
    """Own and derive paths without granting access outside declared roots.

    `ax_source_root` is discovered through :class:`AgentTeamLayout`; mutable
    runtime paths are intentionally external.  Target checkout validation is
    performed at registration time because targets are dynamic.
    """

    ax_source_root: Path
    ax_runtime_root: Path
    wsl_mount_root: PurePosixPath = PurePosixPath("/mnt")

    def __post_init__(self) -> None:
        source = _resolved(self.ax_source_root)
        runtime = _resolved(self.ax_runtime_root)
        mount = PurePosixPath(self.wsl_mount_root)
        if not mount.is_absolute() or ".." in mount.parts or mount == PurePosixPath("/"):
            raise AxPathError("wsl_mount_root must be a bounded absolute POSIX path")
        if _overlaps(source, runtime):
            raise AxPathError(
                "AX source and mutable runtime roots must be disjoint: "
                f"{source} <-> {runtime}"
            )
        object.__setattr__(self, "ax_source_root", source)
        object.__setattr__(self, "ax_runtime_root", runtime)
        object.__setattr__(self, "wsl_mount_root", mount)

    @classmethod
    def discover(
        cls,
        ax_runtime_root: str | Path,
        *,
        anchor: Path | None = None,
        wsl_mount_root: str | PurePosixPath = "/mnt",
    ) -> "AxPathAuthority":
        layout = AgentTeamLayout.discover(anchor)
        return cls.from_layout(
            layout,
            ax_runtime_root,
            wsl_mount_root=wsl_mount_root,
        )

    @classmethod
    def from_layout(
        cls,
        layout: AgentTeamLayout,
        ax_runtime_root: str | Path,
        *,
        wsl_mount_root: str | PurePosixPath = "/mnt",
    ) -> "AxPathAuthority":
        if not isinstance(layout, AgentTeamLayout):
            raise AxPathError("layout must be an AgentTeamLayout")
        return cls(
            ax_source_root=layout.source_root,
            ax_runtime_root=Path(ax_runtime_root),
            wsl_mount_root=PurePosixPath(wsl_mount_root),
        )

    @property
    def state_root(self) -> Path:
        return self._within_runtime(self.ax_runtime_root / "state")

    @property
    def state_database(self) -> Path:
        return self._within_runtime(self.state_root / "agent-team.db")

    @property
    def lock_root(self) -> Path:
        return self._within_runtime(self.state_root / "locks")

    @property
    def repositories_root(self) -> Path:
        return self._within_runtime(self.ax_runtime_root / "repositories")

    @property
    def workspaces_root(self) -> Path:
        return self._within_runtime(self.ax_runtime_root / "workspaces")

    @property
    def activations_root(self) -> Path:
        return self._within_runtime(self.ax_runtime_root / "activations")

    @property
    def artifacts_root(self) -> Path:
        return self._within_runtime(self.ax_runtime_root / "artifacts")

    # Compatibility aliases expose the old names without recreating the old
    # targets/goals/sandboxes hierarchy.  All mutable paths still resolve to the
    # single canonical AX_ROOT layout above.
    @property
    def targets_root(self) -> Path:
        return self.repositories_root

    @property
    def goals_root(self) -> Path:
        return self.workspaces_root

    @property
    def sandboxes_root(self) -> Path:
        return self.activations_root

    def target_runtime(self, target_id: str) -> Path:
        return self._within_runtime(
            self.repositories_root / require_identifier(target_id, "target_id")
        )

    def managed_repository(self, repo_id: str) -> Path:
        repository = require_identifier(repo_id, "repo_id")
        return self._within_runtime(self.repositories_root / f"{repository}.git")

    def target_registry_file(self, target_id: str) -> Path:
        target = require_identifier(target_id, "target_id")
        return self._within_runtime(self.state_root / "repositories" / f"{target}.json")

    def goal_runtime(self, goal_id: str) -> Path:
        return self._within_runtime(
            self.workspaces_root / require_identifier(goal_id, "goal_id")
        )

    def workspace(self, goal_id: str, run_id: str, lease_id: str) -> Path:
        """Return the only legal mutable worktree path for a runtime lease."""

        return self._within_runtime(
            self.workspaces_root
            / require_identifier(goal_id, "goal_id")
            / require_identifier(run_id, "run_id")
            / require_identifier(lease_id, "lease_id")
        )

    def development_worktree(
        self,
        goal_id: str,
        work_item_id: str,
        revision: int,
    ) -> Path:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise AxPathError("revision must be a positive integer")
        work_item = require_identifier(work_item_id, "work_item_id")
        return self.workspace(goal_id, "legacy", f"{work_item}-r{revision}")

    def integration_worktree(self, goal_id: str, attempt_id: str) -> Path:
        attempt = require_identifier(attempt_id, "attempt_id")
        return self.workspace(goal_id, "integration", attempt)

    def activation_root(self, *identifiers: str) -> Path:
        """Return ``activations/<activation>``.

        The two-argument form is retained for old callers that supplied a goal
        ID; the goal never changes the activation-scoped authority root.
        """

        if len(identifiers) == 1:
            activation_id = identifiers[0]
        elif len(identifiers) == 2:
            _, activation_id = identifiers
        else:
            raise AxPathError("activation_root expects activation_id")
        return self._within_runtime(
            self.activations_root
            / require_identifier(activation_id, "activation_id")
        )

    def review_sandbox(self, activation_id: str) -> Path:
        return self.activation_root(activation_id)

    def review_source_root(self, activation_id: str) -> Path:
        return self._within_runtime(self.review_sandbox(activation_id) / "source")

    def scratch_root(self, activation_id: str) -> Path:
        return self._within_runtime(self.review_sandbox(activation_id) / "temp")

    def output_root(self, activation_id: str) -> Path:
        return self._within_runtime(self.review_sandbox(activation_id) / "outputs")

    def artifact_root(self, goal_id: str, evidence_id: str) -> Path:
        return self._within_runtime(
            self.artifacts_root
            / require_identifier(goal_id, "goal_id")
            / require_identifier(evidence_id, "evidence_id")
        )

    def assert_runtime_outside_target(
        self,
        target_checkout: str | Path,
        *,
        git_common_dir: str | Path | None = None,
    ) -> None:
        checkout = _resolved(target_checkout)
        authorities = [checkout]
        if git_common_dir is not None:
            authorities.append(_resolved(git_common_dir))
        for authority in authorities:
            if _overlaps(self.ax_runtime_root, authority):
                raise AxPathError(
                    "AX mutable runtime must be disjoint from target checkout/Git "
                    f"authority: {self.ax_runtime_root} <-> {authority}"
                )
            if _overlaps(self.ax_source_root, authority):
                raise AxPathError(
                    "AX source must be disjoint from target checkout/Git authority: "
                    f"{self.ax_source_root} <-> {authority}"
                )

    def assert_target_path(
        self,
        target_checkout: str | Path,
        candidate: str | Path,
    ) -> Path:
        checkout = _resolved(target_checkout)
        self.assert_runtime_outside_target(checkout)
        resolved = _resolved(candidate)
        if not _contains(checkout, resolved):
            raise AxPathError(f"path escapes target checkout: {resolved}")
        return resolved

    def assert_runtime_path(self, candidate: str | Path) -> Path:
        return self._within_runtime(Path(candidate))

    def canonical_git_common_dir(self, target_checkout: str | Path) -> Path | None:
        checkout = _resolved(target_checkout)
        if not checkout.is_dir():
            raise AxPathError(f"target checkout is not a directory: {checkout}")

        dot_git = checkout / ".git"
        if dot_git.is_dir():
            return dot_git.resolve()
        if dot_git.is_file():
            try:
                marker = dot_git.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise AxPathError(f"cannot read Git indirection file: {dot_git}") from exc
            if not marker.lower().startswith("gitdir:"):
                raise AxPathError(f"invalid Git indirection file: {dot_git}")
            raw_git_dir = marker.split(":", 1)[1].strip()
            if not raw_git_dir:
                raise AxPathError(f"empty Git directory indirection: {dot_git}")
            git_dir = Path(raw_git_dir)
            if not git_dir.is_absolute():
                git_dir = dot_git.parent / git_dir
            git_dir = git_dir.resolve()
            common_marker = git_dir / "commondir"
            if common_marker.is_file():
                try:
                    raw_common = common_marker.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError) as exc:
                    raise AxPathError(
                        f"cannot read Git common-dir marker: {common_marker}"
                    ) from exc
                if not raw_common:
                    raise AxPathError(f"empty Git common-dir marker: {common_marker}")
                common_dir = Path(raw_common)
                if not common_dir.is_absolute():
                    common_dir = git_dir / common_dir
                return common_dir.resolve()
            return git_dir
        if (checkout / "HEAD").is_file() and (checkout / "objects").is_dir():
            return checkout
        return None

    def canonical_target_identity(self, target_checkout: str | Path) -> str:
        checkout = _resolved(target_checkout)
        common_dir = self.canonical_git_common_dir(checkout)
        identity_path = common_dir or checkout
        self.assert_runtime_outside_target(
            checkout,
            git_common_dir=common_dir,
        )
        digest = hashlib.sha256(
            _canonical_path_key(identity_path).encode("utf-8")
        ).hexdigest()
        return f"target-{digest[:24]}"

    def to_wsl_path(self, windows_path: str | Path | PureWindowsPath) -> str:
        raw = str(windows_path)
        path = PureWindowsPath(raw)
        if raw.startswith("\\\\") or path.drive.startswith("\\\\"):
            raise AxPathError("UNC and device paths require an explicit mount mapping")
        if (
            not path.is_absolute()
            or not re.fullmatch(r"[A-Za-z]:", path.drive)
            or path.anchor in {"", "\\"}
        ):
            raise AxPathError(f"path is not an absolute drive-letter Windows path: {raw}")
        if "\x00" in raw:
            raise AxPathError("path contains a NUL character")
        drive = path.drive[0].lower()
        relative_parts = path.parts[1:]
        translated = self.wsl_mount_root / drive
        for part in relative_parts:
            if part in {"", ".", ".."} or "/" in part:
                raise AxPathError(f"path contains an unsafe component: {part!r}")
            translated /= part
        return translated.as_posix()

    def from_wsl_path(self, wsl_path: str | PurePosixPath) -> PureWindowsPath:
        raw = str(wsl_path)
        path = PurePosixPath(raw)
        if not path.is_absolute() or "\x00" in raw or ".." in path.parts:
            raise AxPathError(f"path is not a safe absolute WSL path: {raw}")
        try:
            relative = path.relative_to(self.wsl_mount_root)
        except ValueError as exc:
            raise AxPathError(
                f"WSL path is outside configured mount root {self.wsl_mount_root}: {raw}"
            ) from exc
        if len(relative.parts) < 1 or not re.fullmatch(
            r"[A-Za-z]", relative.parts[0]
        ):
            raise AxPathError(f"WSL path does not include a drive mount: {raw}")
        drive = relative.parts[0].upper()
        return PureWindowsPath(f"{drive}:\\", *relative.parts[1:])

    def _within_runtime(self, candidate: Path) -> Path:
        resolved = _resolved(candidate)
        if not _contains(self.ax_runtime_root, resolved):
            raise AxPathError(f"path escapes AX runtime root: {resolved}")
        return resolved
