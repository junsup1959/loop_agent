from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .agent_team_queue import SQLiteMessageQueue
    from .project_agents import (
        AgentConfigurationError,
        load_and_validate as load_and_validate_agents,
        resolve_binding,
    )
except ImportError:
    from agent_team_queue import SQLiteMessageQueue
    from project_agents import (
        AgentConfigurationError,
        load_and_validate as load_and_validate_agents,
        resolve_binding,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTEXT_PROFILE_PATH = PROJECT_ROOT / "agents" / "context-profiles.toml"
PROFILE_INTEGER_FIELDS = {
    "expansion_level",
    "max_messages",
    "max_message_chars",
    "max_snapshot_chars",
    "max_paths",
    "max_diff_chars",
    "max_commits",
    "max_skills",
    "max_skill_chars",
    "max_packet_chars",
}
MESSAGE_METADATA_FIELDS = (
    "seq",
    "id",
    "parent_message_id",
    "from_role",
    "to_role",
    "type",
    "priority",
    "created_at",
)
SNAPSHOT_METADATA_FIELDS = (
    "id",
    "thread_id",
    "work_item_id",
    "target_role",
    "covered_through_seq",
    "created_at",
)
PATH_PAYLOAD_KEYS = {
    "changed_paths",
    "context_paths",
    "path",
    "paths",
    "write_scope",
}
ACTION_PAYLOAD_KEYS = {
    "acceptance_criteria",
    "decision",
    "finding",
    "findings",
    "question",
    "required_action",
    "review_type",
    "task",
    "work_contract",
}


class ContextSelectionError(ValueError):
    """Raised when a requested context selection is invalid or unsafe."""


class ContextBudgetError(ContextSelectionError):
    """Raised when the minimum bounded packet cannot fit its declared policy."""


class GitContextError(RuntimeError):
    """Raised when immutable Git context cannot be reconstructed."""


@dataclass(frozen=True)
class RepositoryConfig:
    repo_id: str
    bare_repo: Path
    default_branch: str = "integration"
    index_path: Path | None = None


@dataclass(frozen=True)
class ContextBudget:
    profile: str
    roles: frozenset[str]
    expansion_level: int
    max_messages: int
    max_message_chars: int
    max_snapshot_chars: int
    max_paths: int
    max_diff_chars: int
    max_commits: int
    max_skills: int
    max_skill_chars: int
    max_packet_chars: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "profile": self.profile,
            "expansion_level": self.expansion_level,
            "max_messages": self.max_messages,
            "max_message_chars": self.max_message_chars,
            "max_snapshot_chars": self.max_snapshot_chars,
            "max_paths": self.max_paths,
            "max_diff_chars": self.max_diff_chars,
            "max_commits": self.max_commits,
            "max_skills": self.max_skills,
            "max_skill_chars": self.max_skill_chars,
            "max_packet_chars": self.max_packet_chars,
        }


class ContextProfileCatalog:
    """Loads fail-closed role/profile budgets from project-local TOML."""

    def __init__(self, path: str | Path = CONTEXT_PROFILE_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ContextSelectionError(f"Context profile file is missing: {self.path}")
        with self.path.open("rb") as stream:
            data = tomllib.load(stream)
        context = data.get("context")
        role_defaults = data.get("role_defaults")
        profiles = data.get("profiles")
        if not isinstance(context, dict) or context.get("version") != 1:
            raise ContextSelectionError("Context profile catalog must declare version = 1.")
        if not isinstance(role_defaults, dict) or not role_defaults:
            raise ContextSelectionError("Context profile catalog must define role_defaults.")
        if not isinstance(profiles, dict) or not profiles:
            raise ContextSelectionError("Context profile catalog must define profiles.")

        self.default_profile = context.get("default_profile", "auto")
        if not isinstance(self.default_profile, str) or not self.default_profile:
            raise ContextSelectionError("context.default_profile must be a non-empty string.")
        self.role_defaults = self._validate_role_defaults(role_defaults, profiles)
        self.profiles = self._validate_profiles(profiles)

    @staticmethod
    def _validate_role_defaults(
        role_defaults: Mapping[str, Any], profiles: Mapping[str, Any]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for role, profile in role_defaults.items():
            if not isinstance(role, str) or not role:
                raise ContextSelectionError("Context role default keys must be non-empty strings.")
            if not isinstance(profile, str) or profile not in profiles:
                raise ContextSelectionError(
                    f"Context role default for {role!r} references an unknown profile."
                )
            result[role] = profile
        return result

    @staticmethod
    def _integer(raw: Mapping[str, Any], field: str) -> int:
        value = raw.get(field)
        if not isinstance(value, int) or value < 0:
            raise ContextSelectionError(
                f"Context profile field {field!r} must be a non-negative integer."
            )
        return value

    def _validate_profiles(self, profiles: Mapping[str, Any]) -> dict[str, ContextBudget]:
        result: dict[str, ContextBudget] = {}
        for profile, raw in profiles.items():
            if not isinstance(profile, str) or not profile:
                raise ContextSelectionError("Context profile names must be non-empty strings.")
            if not isinstance(raw, dict):
                raise ContextSelectionError(f"Context profile {profile!r} must be a table.")
            roles = raw.get("roles")
            if (
                not isinstance(roles, list)
                or not roles
                or not all(isinstance(role, str) and role for role in roles)
            ):
                raise ContextSelectionError(
                    f"Context profile {profile!r} must declare one or more roles."
                )
            unknown_fields = set(raw) - {"roles", *PROFILE_INTEGER_FIELDS}
            if unknown_fields:
                raise ContextSelectionError(
                    f"Context profile {profile!r} has unknown fields: "
                    f"{sorted(unknown_fields)}"
                )
            values = {field: self._integer(raw, field) for field in PROFILE_INTEGER_FIELDS}
            if values["expansion_level"] < 1 or values["expansion_level"] > 4:
                raise ContextSelectionError(
                    f"Context profile {profile!r} expansion_level must be between 1 and 4."
                )
            if values["max_packet_chars"] == 0:
                raise ContextSelectionError(
                    f"Context profile {profile!r} max_packet_chars must be positive."
                )
            result[profile] = ContextBudget(
                profile=profile,
                roles=frozenset(roles),
                **values,
            )
        return result

    def resolve(self, *, target_role: str, requested_profile: str | None) -> ContextBudget:
        profile = requested_profile or self.default_profile
        if profile in {"auto", "default"}:
            try:
                profile = self.role_defaults[target_role]
            except KeyError as exc:
                raise ContextSelectionError(
                    f"No default context profile exists for role {target_role!r}."
                ) from exc
        budget = self.profiles.get(profile)
        if budget is None:
            raise ContextSelectionError(f"Unknown context profile: {profile!r}")
        if target_role not in budget.roles:
            raise ContextSelectionError(
                f"Context profile {profile!r} is not allowed for role {target_role!r}."
            )
        return budget


class RepositoryRegistry:
    def __init__(self, registry_path: str | Path) -> None:
        self.registry_path = Path(registry_path).expanduser().resolve()
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        repositories = data.get("repositories")
        if not isinstance(repositories, dict):
            raise ValueError("registry must contain a 'repositories' object")
        self._repositories: dict[str, RepositoryConfig] = {}
        for repo_id, value in repositories.items():
            if not isinstance(value, dict) or "bare_repo" not in value:
                raise ValueError(f"repository {repo_id!r} must define bare_repo")
            bare_repo = self._resolve_path(value["bare_repo"])
            index_path = (
                self._resolve_path(value["index_path"]) if value.get("index_path") else None
            )
            self._repositories[repo_id] = RepositoryConfig(
                repo_id=repo_id,
                bare_repo=bare_repo,
                default_branch=value.get("default_branch", "integration"),
                index_path=index_path,
            )

    def _resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.registry_path.parent / path
        return path.resolve()

    def get(self, repo_id: str) -> RepositoryConfig:
        try:
            return self._repositories[repo_id]
        except KeyError as exc:
            raise KeyError(f"unknown repo_id: {repo_id}") from exc


class GitContextReader:
    def __init__(self, repository: RepositoryConfig) -> None:
        self.repository = repository
        if not repository.bare_repo.exists():
            raise GitContextError(f"repository does not exist: {repository.bare_repo}")

    def _git(self, *arguments: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", f"--git-dir={self.repository.bare_repo}", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            raise GitContextError(
                f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
            )
        return result.stdout

    @staticmethod
    def normalize_relative_path(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ContextSelectionError("Context paths must be non-empty strings.")
        raw = value.replace("\\", "/")
        path = PurePosixPath(raw)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or (path.parts and ":" in path.parts[0])
        ):
            raise ContextSelectionError(
                f"Context path must be repository-relative and normalized: {value!r}"
            )
        return path.as_posix()

    def verify_commit(self, oid: str) -> str:
        full_oid = self._git("rev-parse", "--verify", f"{oid}^{{commit}}").strip()
        if not full_oid:
            raise GitContextError(f"commit cannot be resolved: {oid}")
        return full_oid

    def changed_paths(self, base_oid: str, head_oid: str) -> list[dict[str, str]]:
        output = self._git(
            "diff",
            "--name-status",
            "--find-renames",
            base_oid,
            head_oid,
        )
        changes: list[dict[str, str]] = []
        for line in output.splitlines():
            if not line:
                continue
            fields = line.split("\t")
            status = fields[0]
            if status.startswith(("R", "C")) and len(fields) >= 3:
                changes.append(
                    {"status": status, "old_path": fields[1], "path": fields[2]}
                )
            elif len(fields) >= 2:
                changes.append({"status": status, "path": fields[1]})
        return changes

    def diff_stat(
        self,
        base_oid: str,
        head_oid: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> str:
        arguments = ["diff", "--stat", "--find-renames", base_oid, head_oid]
        if paths is not None:
            arguments.extend(["--", *paths])
        return self._git(*arguments)

    def diff_text(
        self,
        base_oid: str,
        head_oid: str,
        *,
        max_chars: int = 120_000,
        paths: Sequence[str] | None = None,
    ) -> tuple[str, bool]:
        arguments = [
            "diff",
            "--find-renames",
            "--function-context",
            base_oid,
            head_oid,
        ]
        if paths is not None:
            arguments.extend(["--", *paths])
        output = self._git(*arguments)
        if len(output) <= max_chars:
            return output, False
        return output[:max_chars], True

    def commit_series(self, base_oid: str, head_oid: str) -> list[dict[str, str]]:
        separator = "\x1f"
        output = self._git(
            "log",
            "--reverse",
            f"--format=%H{separator}%P{separator}%an{separator}%aI{separator}%s",
            f"{base_oid}..{head_oid}",
        )
        commits: list[dict[str, str]] = []
        for line in output.splitlines():
            fields = line.split(separator, 4)
            if len(fields) == 5:
                commits.append(
                    {
                        "oid": fields[0],
                        "parents": fields[1],
                        "author": fields[2],
                        "authored_at": fields[3],
                        "subject": fields[4],
                    }
                )
        return commits

    @staticmethod
    def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
        if len(value) <= max_chars:
            return value, False
        return value[:max_chars], True

    def build_change_context(
        self,
        *,
        base_oid: str,
        head_oid: str,
        max_diff_chars: int = 120_000,
        max_paths: int | None = None,
        max_commits: int | None = None,
        paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if max_diff_chars < 0:
            raise ContextSelectionError("max_diff_chars must be non-negative.")
        if max_paths is not None and max_paths < 0:
            raise ContextSelectionError("max_paths must be non-negative.")
        if max_commits is not None and max_commits < 0:
            raise ContextSelectionError("max_commits must be non-negative.")

        verified_base = self.verify_commit(base_oid)
        verified_head = self.verify_commit(head_oid)
        all_changes = self.changed_paths(verified_base, verified_head)
        change_by_path = {change["path"]: change for change in all_changes}
        all_paths = list(change_by_path)

        if paths is None:
            requested_paths = list(all_paths)
        else:
            requested_paths = []
            for raw_path in paths:
                normalized = self.normalize_relative_path(raw_path)
                if normalized not in change_by_path:
                    raise ContextSelectionError(
                        "Context path is not present in the authoritative Git delta: "
                        f"{normalized}"
                    )
                if normalized not in requested_paths:
                    requested_paths.append(normalized)

        selected_paths = (
            requested_paths[:max_paths] if max_paths is not None else requested_paths
        )
        selected_changes = [change_by_path[path] for path in selected_paths]
        all_commits = self.commit_series(verified_base, verified_head)
        if max_commits is None:
            selected_commits = all_commits
        elif max_commits == 0:
            selected_commits = []
        else:
            selected_commits = all_commits[-max_commits:]

        if selected_paths:
            diff, diff_truncated = self.diff_text(
                verified_base,
                verified_head,
                max_chars=max_diff_chars,
                paths=selected_paths,
            ) if max_diff_chars else ("", False)
            diff_stat, diff_stat_truncated = self._truncate_text(
                self.diff_stat(verified_base, verified_head, paths=selected_paths),
                max_diff_chars,
            ) if max_diff_chars else ("", False)
        else:
            diff = ""
            diff_truncated = False
            diff_stat = ""
            diff_stat_truncated = False

        return {
            "repo_id": self.repository.repo_id,
            "bare_repo": str(self.repository.bare_repo),
            "base_oid": verified_base,
            "head_oid": verified_head,
            "changed_paths": selected_changes,
            "changed_path_count": len(all_changes),
            "selected_paths": selected_paths,
            "omitted_changed_path_count": len(all_changes) - len(selected_changes),
            "diff_stat": diff_stat,
            "diff_stat_truncated": diff_stat_truncated,
            "commit_series": selected_commits,
            "commit_count": len(all_commits),
            "omitted_commit_count": len(all_commits) - len(selected_commits),
            "diff": diff,
            "diff_truncated": diff_truncated,
            "diff_omitted": not bool(max_diff_chars and selected_paths),
            "semantic_index": (
                str(self.repository.index_path)
                if self.repository.index_path is not None
                else None
            ),
        }


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded_value(value: Any, max_chars: int) -> tuple[Any, int, bool]:
    serialized = _json_text(value)
    if len(serialized) <= max_chars:
        return value, len(serialized), False
    if max_chars <= 0:
        excerpt = ""
    else:
        excerpt = serialized[:max_chars]
    return (
        {
            "context_excerpt": excerpt,
            "original_chars": len(serialized),
            "truncated": True,
        },
        len(serialized),
        True,
    )


def _payload_mentions_action(payload: Any) -> bool:
    return isinstance(payload, Mapping) and any(key in payload for key in ACTION_PAYLOAD_KEYS)


def _message_rank(message: Mapping[str, Any], target_role: str) -> tuple[int, int, int, int]:
    direct_inbound = 0 if message.get("to_role") == target_role else 1
    action = 0 if _payload_mentions_action(message.get("payload")) else 1
    priority = message.get("priority", 0)
    sequence = message.get("seq", 0)
    return (direct_inbound, action, -int(priority), -int(sequence))


def _project_snapshot(
    snapshot: Mapping[str, Any] | None,
    *,
    max_chars: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if snapshot is None:
        return None, []
    projected = {field: snapshot.get(field) for field in SNAPSHOT_METADATA_FIELDS}
    payload, original_chars, truncated = _bounded_value(snapshot.get("payload", {}), max_chars)
    projected["payload"] = payload
    if not truncated:
        return projected, []
    return projected, [
        {
            "kind": "snapshot_payload",
            "reason": "max_snapshot_chars",
            "original_chars": original_chars,
        }
    ]


def _select_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    target_role: str,
    max_messages: int,
    max_message_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ranked = sorted(messages, key=lambda message: _message_rank(message, target_role))
    selected = ranked[:max_messages]
    selected.sort(key=lambda message: int(message.get("seq", 0)))
    remaining_chars = max_message_chars
    projected: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    selection_reasons: list[dict[str, Any]] = []

    for message in selected:
        result = {field: message.get(field) for field in MESSAGE_METADATA_FIELDS}
        payload, original_chars, truncated = _bounded_value(
            message.get("payload", {}), max(remaining_chars, 0)
        )
        result["payload"] = payload
        projected.append(result)
        remaining_chars = max(0, remaining_chars - min(original_chars, remaining_chars))
        if truncated:
            omissions.append(
                {
                    "kind": "message_payload",
                    "message_id": message.get("id"),
                    "reason": "max_message_chars",
                    "original_chars": original_chars,
                }
            )
        selection_reasons.append(
            {
                "message_id": message.get("id"),
                "reason": (
                    "direct_inbound"
                    if message.get("to_role") == target_role
                    else "target_role_history"
                ),
            }
        )

    if len(messages) > len(selected):
        omissions.append(
            {
                "kind": "messages",
                "reason": "max_messages",
                "omitted_count": len(messages) - len(selected),
            }
        )
    return projected, omissions, selection_reasons


def _extract_payload_paths(payload: Any) -> list[str]:
    candidates: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, Mapping):
            for key in ("path", "old_path"):
                if isinstance(value.get(key), str):
                    candidates.append(value[key])
        elif isinstance(value, list):
            for item in value:
                add(item)

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key in PATH_PAYLOAD_KEYS:
                add(value)
    return candidates


def _select_paths(
    *,
    changes: Sequence[Mapping[str, str]],
    selected_messages: Sequence[Mapping[str, Any]],
    explicit_paths: Sequence[str] | None,
    max_paths: int,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, Any]]]:
    authoritative_paths = [change["path"] for change in changes]
    authoritative_set = set(authoritative_paths)
    candidate_paths: list[str] = []
    selection_reasons: list[dict[str, str]] = []

    if explicit_paths is not None:
        source = "explicit_context_paths"
        raw_paths = list(explicit_paths)
    else:
        source = "message_changed_paths"
        raw_paths = []
        for message in selected_messages:
            raw_paths.extend(_extract_payload_paths(message.get("payload", {})))
        if not raw_paths:
            source = "git_delta_fallback"
            raw_paths = authoritative_paths

    for raw_path in raw_paths:
        normalized = GitContextReader.normalize_relative_path(raw_path)
        if normalized not in authoritative_set:
            if explicit_paths is not None:
                raise ContextSelectionError(
                    "Explicit context path is not present in the authoritative Git delta: "
                    f"{normalized}"
                )
            continue
        if normalized not in candidate_paths:
            candidate_paths.append(normalized)
            selection_reasons.append({"path": normalized, "reason": source})

    selected_paths = candidate_paths[:max_paths]
    omissions: list[dict[str, Any]] = []
    if len(authoritative_paths) > len(selected_paths):
        omissions.append(
            {
                "kind": "changed_paths",
                "reason": "max_paths" if len(candidate_paths) > max_paths else "not_selected",
                "omitted_count": len(authoritative_paths) - len(selected_paths),
            }
        )
    return selected_paths, selection_reasons[:max_paths], omissions


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_skill_ids(value: Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    skill_ids = list(value)
    if not all(isinstance(skill_id, str) and skill_id for skill_id in skill_ids):
        raise ContextSelectionError("Selected skill IDs must be non-empty strings.")
    if len(skill_ids) != len(set(skill_ids)):
        raise ContextSelectionError("Selected skill IDs must be unique.")
    return skill_ids


def _project_relative_path(value: str) -> Path:
    candidate = (PROJECT_ROOT / value).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ContextSelectionError(f"Skill path escapes the project root: {value}") from exc
    return candidate


def materialize_skill_instructions(skill_packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read exactly the already-selected skill files for one runner activation."""

    skills = skill_packet.get("skills", [])
    max_skill_chars = skill_packet.get("max_content_chars", 0)
    if not isinstance(skills, list) or not isinstance(max_skill_chars, int):
        raise ContextSelectionError("Invalid selected skill packet.")
    remaining_chars = max_skill_chars
    result: list[dict[str, Any]] = []
    for skill in skills:
        if not isinstance(skill, Mapping):
            raise ContextSelectionError("Selected skill metadata must be an object.")
        skill_path = skill.get("skill_md")
        expected_hash = skill.get("sha256")
        expected_chars = skill.get("content_chars")
        if (
            not isinstance(skill_path, str)
            or not isinstance(expected_hash, str)
            or not isinstance(expected_chars, int)
        ):
            raise ContextSelectionError("Selected skill metadata is incomplete.")
        source = _project_relative_path(skill_path)
        if not source.is_file():
            raise ContextSelectionError(f"Selected skill file is missing: {source}")
        content = source.read_text(encoding="utf-8")
        if len(content) != expected_chars or _sha256_text(content) != expected_hash:
            raise ContextSelectionError(
                f"Selected skill content changed after context compilation: {skill_path}"
            )
        if len(content) > remaining_chars:
            raise ContextBudgetError(
                "Selected skill content exceeds the compiled context budget."
            )
        remaining_chars -= len(content)
        result.append(
            {
                "id": skill.get("id"),
                "kind": skill.get("kind"),
                "content": content,
                "sha256": expected_hash,
            }
        )
    return result


class ContextCompiler:
    """Compiles an immutable, role-specific minimum context packet."""

    def __init__(
        self,
        *,
        queue: SQLiteMessageQueue,
        registry: RepositoryRegistry,
        profile_catalog: ContextProfileCatalog | None = None,
    ) -> None:
        self.queue = queue
        self.registry = registry
        self.profile_catalog = profile_catalog or ContextProfileCatalog()

    @staticmethod
    def _clamp_limit(
        requested: int | None,
        configured: int,
        *,
        field: str,
        omissions: list[dict[str, Any]],
    ) -> int:
        if requested is None:
            return configured
        if not isinstance(requested, int) or requested < 0:
            raise ContextSelectionError(f"{field} must be a non-negative integer.")
        applied = min(requested, configured)
        if requested > configured:
            omissions.append(
                {
                    "kind": "requested_limit",
                    "field": field,
                    "reason": "profile_cap",
                    "requested": requested,
                    "applied": applied,
                }
            )
        return applied

    @staticmethod
    def _resolve_skill_packet(
        *,
        target_role: str,
        actor_seat_id: str | None,
        skill_ids: Sequence[str],
        budget: ContextBudget,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if len(skill_ids) > budget.max_skills:
            raise ContextBudgetError(
                f"Profile {budget.profile!r} permits at most {budget.max_skills} skills."
            )
        if actor_seat_id is None:
            if skill_ids:
                raise ContextSelectionError(
                    "actor_seat_id is required when selected_skill_ids are supplied."
                )
            return {
                "scope": "project",
                "role": target_role,
                "explicit_injection": True,
                "max_content_chars": budget.max_skill_chars,
                "skills": [],
            }, None
        try:
            bundle = load_and_validate_agents()
            seat = bundle["seats"].get(actor_seat_id)
            if seat is None:
                raise AgentConfigurationError(f"Unknown seat: {actor_seat_id}")
            if seat["role_key"] != target_role:
                raise ContextSelectionError(
                    f"Seat {actor_seat_id!r} belongs to role {seat['role_key']!r}, "
                    f"not {target_role!r}."
                )
            if skill_ids:
                binding = resolve_binding(bundle, actor_seat_id, list(skill_ids))
                skill_packet = dict(binding["skill_packet"])
            else:
                binding = {
                    "scope": "project",
                    "seat_id": actor_seat_id,
                    "role_key": target_role,
                    "agent_file": f".codex/agents/{actor_seat_id}.toml",
                }
                skill_packet = {
                    "scope": "project",
                    "role": target_role,
                    "explicit_injection": True,
                    "skills": [],
                }
        except AgentConfigurationError as exc:
            raise ContextSelectionError(str(exc)) from exc

        projected_skills: list[dict[str, Any]] = []
        total_chars = 0
        for descriptor in skill_packet["skills"]:
            source = _project_relative_path(descriptor["skill_md"])
            if not source.is_file():
                raise ContextSelectionError(f"Selected skill file is missing: {source}")
            content = source.read_text(encoding="utf-8")
            total_chars += len(content)
            projected_skills.append(
                {
                    "id": descriptor["id"],
                    "kind": descriptor["kind"],
                    "skill_md": descriptor["skill_md"],
                    "content_chars": len(content),
                    "sha256": _sha256_text(content),
                }
            )
        if total_chars > budget.max_skill_chars:
            raise ContextBudgetError(
                "Selected skill content exceeds the profile skill-content budget."
            )
        return {
            "scope": "project",
            "role": target_role,
            "explicit_injection": True,
            "max_content_chars": budget.max_skill_chars,
            "skills": projected_skills,
        }, binding

    def compile(
        self,
        *,
        thread_id: str,
        work_item_id: str,
        target_role: str,
        repo_id: str,
        base_oid: str,
        head_oid: str,
        context_profile: str | None,
        context_action: str | None = None,
        actor_seat_id: str | None = None,
        selected_skill_ids: Sequence[str] | None = None,
        max_messages: int | None = None,
        max_diff_chars: int | None = None,
        paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if not all(
            isinstance(value, str) and value
            for value in (thread_id, work_item_id, target_role, repo_id, base_oid, head_oid)
        ):
            raise ContextSelectionError("Context identity and Git OID fields must be non-empty strings.")
        if context_action is not None and (not isinstance(context_action, str) or not context_action):
            raise ContextSelectionError("context_action must be a non-empty string when supplied.")
        if actor_seat_id is not None and (
            not isinstance(actor_seat_id, str) or not actor_seat_id
        ):
            raise ContextSelectionError("actor_seat_id must be a non-empty string when supplied.")

        budget = self.profile_catalog.resolve(
            target_role=target_role, requested_profile=context_profile
        )
        omissions: list[dict[str, Any]] = []
        applied_messages = self._clamp_limit(
            max_messages,
            budget.max_messages,
            field="max_messages",
            omissions=omissions,
        )
        applied_diff_chars = self._clamp_limit(
            max_diff_chars,
            budget.max_diff_chars,
            field="max_diff_chars",
            omissions=omissions,
        )
        skill_ids = _require_skill_ids(selected_skill_ids)
        skill_packet, agent_binding = self._resolve_skill_packet(
            target_role=target_role,
            actor_seat_id=actor_seat_id,
            skill_ids=skill_ids,
            budget=budget,
        )

        message_scan_limit = max(applied_messages, min(applied_messages * 8, 128))
        raw_message_context = self.queue.context_bundle(
            thread_id=thread_id,
            target_role=target_role,
            work_item_id=work_item_id,
            limit=message_scan_limit,
        )
        snapshot, snapshot_omissions = _project_snapshot(
            raw_message_context["snapshot"], max_chars=budget.max_snapshot_chars
        )
        omissions.extend(snapshot_omissions)
        selected_messages, message_omissions, message_reasons = _select_messages(
            raw_message_context["delta_messages"],
            target_role=target_role,
            max_messages=applied_messages,
            max_message_chars=budget.max_message_chars,
        )
        omissions.extend(message_omissions)
        if raw_message_context["truncated"]:
            omissions.append(
                {
                    "kind": "message_scan",
                    "reason": "scan_limit",
                    "limit": message_scan_limit,
                }
            )

        repository = self.registry.get(repo_id)
        git_reader = GitContextReader(repository)
        verified_base = git_reader.verify_commit(base_oid)
        verified_head = git_reader.verify_commit(head_oid)
        authoritative_changes = git_reader.changed_paths(verified_base, verified_head)
        selected_paths, path_reasons, path_omissions = _select_paths(
            changes=authoritative_changes,
            selected_messages=selected_messages,
            explicit_paths=paths,
            max_paths=budget.max_paths,
        )
        omissions.extend(path_omissions)
        git_context = git_reader.build_change_context(
            base_oid=verified_base,
            head_oid=verified_head,
            max_diff_chars=applied_diff_chars,
            max_paths=budget.max_paths,
            max_commits=budget.max_commits,
            paths=selected_paths,
        )
        if git_context["diff_truncated"]:
            omissions.append(
                {
                    "kind": "git_diff",
                    "reason": "max_diff_chars",
                    "limit": applied_diff_chars,
                }
            )
        if git_context["omitted_commit_count"]:
            omissions.append(
                {
                    "kind": "commit_series",
                    "reason": "max_commits",
                    "omitted_count": git_context["omitted_commit_count"],
                }
            )
        if git_context["diff_omitted"] and authoritative_changes:
            omissions.append(
                {
                    "kind": "git_diff",
                    "reason": "profile_excludes_diff_or_paths",
                }
            )

        packet = {
            "context_version": 2,
            "thread_id": thread_id,
            "work_item_id": work_item_id,
            "target_role": target_role,
            "target_seat_id": actor_seat_id,
            "context_profile": budget.profile,
            "context_action": context_action or budget.profile,
            "context_budget": {
                **budget.as_dict(),
                "applied_max_messages": applied_messages,
                "applied_max_diff_chars": applied_diff_chars,
            },
            "agent_binding": agent_binding,
            "skill_packet": skill_packet,
            "message_context": {
                "snapshot": snapshot,
                "delta_messages": selected_messages,
                "selected_message_ids": [message["id"] for message in selected_messages],
                "selection_reasons": message_reasons,
            },
            "git_context": git_context,
            "artifact_refs": [],
            "semantic_evidence": [],
            "selection_reasons": {"paths": path_reasons},
            "omitted_context": omissions,
        }
        skill_content_chars = sum(
            int(skill["content_chars"]) for skill in skill_packet["skills"]
        )
        packet["context_chars"] = 0
        packet["injected_context_chars"] = 0
        for _ in range(3):
            packet_chars = len(_json_text(packet))
            injected_context_chars = packet_chars + skill_content_chars
            if (
                packet["context_chars"] == packet_chars
                and packet["injected_context_chars"] == injected_context_chars
            ):
                break
            packet["context_chars"] = packet_chars
            packet["injected_context_chars"] = injected_context_chars
        if injected_context_chars > budget.max_packet_chars:
            raise ContextBudgetError(
                f"Compiled context and selected skills are {injected_context_chars} chars, "
                f"above the profile limit of {budget.max_packet_chars}. Narrow selected "
                "paths or skills."
            )
        return packet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile the minimum role-specific local context packet."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--work-item", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--seat")
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--base-oid", required=True)
    parser.add_argument("--head-oid", required=True)
    parser.add_argument("--profile", default="auto")
    parser.add_argument("--action")
    parser.add_argument("--skill", action="append", dest="selected_skill_ids")
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--max-diff-chars", type=int)
    parser.add_argument("--path", action="append", dest="paths")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    compiler = ContextCompiler(
        queue=SQLiteMessageQueue(args.db),
        registry=RepositoryRegistry(args.registry),
    )
    result = compiler.compile(
        thread_id=args.thread,
        work_item_id=args.work_item,
        target_role=args.role,
        actor_seat_id=args.seat,
        repo_id=args.repo_id,
        base_oid=args.base_oid,
        head_oid=args.head_oid,
        context_profile=args.profile,
        context_action=args.action,
        selected_skill_ids=args.selected_skill_ids,
        max_messages=args.max_messages,
        max_diff_chars=args.max_diff_chars,
        paths=args.paths,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
