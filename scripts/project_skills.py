#!/usr/bin/env python3
"""Validate, synchronize, and resolve digest-pinned project Skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.agent_team_layout import AgentTeamLayout
except ModuleNotFoundError:
    from agent_team_layout import AgentTeamLayout


LAYOUT = AgentTeamLayout.discover(Path(__file__))
PROJECT_ROOT = LAYOUT.source_root
CATALOG_PATH = LAYOUT.skill_catalog_path
MCP_POLICY_PATH = LAYOUT.config_root / "mcp-policy.toml"
HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
FRONTMATTER_PATTERN = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
NAME_PATTERN = re.compile(r"^name:\s*(?P<name>[a-z0-9-]+)\s*$", re.MULTILINE)
DESCRIPTION_PATTERN = re.compile(r"^description:\s*(?P<description>.+)\s*$", re.MULTILINE)
SHORT_DESCRIPTION_PATTERN = re.compile(
    r'^  short_description:\s*"(?P<description>[^"]+)"\s*$',
    re.MULTILINE,
)
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_KINDS = {"workflow", "professional"}
REQUIRED_MCP_SERVERS = {"serena", "sequentialthinking"}
ENTRY_KEYS = {
    "id",
    "version",
    "sha256",
    "path",
    "kind",
    "eligible_capabilities",
    "content_budget_chars",
    "mcp_prerequisites",
    "summary",
}
REQUIRED_FORBIDDEN_EXPANSIONS = {
    "approval_authorities",
    "authority",
    "gate_authorities",
    "gates",
    "merge",
    "models",
    "sandbox_mode",
    "tools",
    "write_roots",
}


class SkillConfigurationError(RuntimeError):
    """Raised when the project-local Skill configuration is invalid."""


def _read_toml(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SkillConfigurationError(f"{label} not found: {path}")
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise SkillConfigurationError(f"Invalid TOML in {path}: {exc}") from exc


def load_catalog(catalog_path: Path | None = None) -> dict[str, Any]:
    path = Path(catalog_path or CATALOG_PATH).resolve()
    data = _read_toml(path, "Catalog")
    if "catalog" not in data or "skills" not in data:
        raise SkillConfigurationError(
            "Catalog must define [catalog] and at least one [[skills]] entry."
        )
    if not isinstance(data["skills"], list):
        raise SkillConfigurationError("Catalog skills must be an array of tables.")
    return data


def load_mcp_policy(policy_path: Path | None = None) -> dict[str, Any]:
    return _read_toml(Path(policy_path or MCP_POLICY_PATH).resolve(), "MCP policy")


def _inside_root(root: Path, relative: str, label: str) -> Path:
    logical = PurePosixPath(relative.replace("\\", "/"))
    if (
        logical.is_absolute()
        or any(part in {"", ".", ".."} for part in logical.parts)
        or (logical.parts and ":" in logical.parts[0])
    ):
        raise SkillConfigurationError(f"{label} must be a normalized relative path.")
    resolved_root = root.resolve()
    resolved = resolved_root.joinpath(*logical.parts).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SkillConfigurationError(f"{label} escapes the Skill root: {relative}") from exc
    return resolved


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillConfigurationError(f"File is not UTF-8: {path}") from exc


def _validate_english_text(path: Path, text: str) -> None:
    if HANGUL_PATTERN.search(text):
        raise SkillConfigurationError(f"Implementation content must be English: {path}")
    if "TODO" in text:
        raise SkillConfigurationError(f"Unresolved TODO marker in {path}")


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise SkillConfigurationError(f"{label} must be a string list.")
    if any(not isinstance(item, str) or not item for item in value):
        raise SkillConfigurationError(f"{label} contains an invalid value.")
    if len(value) != len(set(value)):
        raise SkillConfigurationError(f"{label} contains duplicates.")
    return tuple(value)


def validate_mcp_policy(
    data: Mapping[str, Any],
    capability_ids: set[str] | None = None,
) -> dict[str, Any]:
    policy = data.get("policy")
    servers = data.get("servers")
    bindings = data.get("capability_bindings")
    required_use = data.get("required_use_bindings")
    if not isinstance(policy, Mapping) or policy.get("version") != 1:
        raise SkillConfigurationError("MCP policy version must be 1.")
    if policy.get("enabled") is not True or policy.get("fallback_allowed") is not False:
        raise SkillConfigurationError("MCP policy must be enabled with no fallback.")
    required_servers = set(
        _string_list(policy.get("required_servers"), "policy.required_servers")
    )
    if required_servers != REQUIRED_MCP_SERVERS:
        raise SkillConfigurationError(
            "Serena and Sequential Thinking must be the required MCP servers."
        )
    for field in (
        "health_preflight_required",
        "tool_preflight_required",
        "usage_receipt_required_when_invoked",
    ):
        if policy.get(field) is not True:
            raise SkillConfigurationError(f"policy.{field} must be true.")
    for field in (
        "skill_may_expand_servers",
        "skill_may_expand_tools",
        "profile_may_expand_servers",
        "profile_may_expand_tools",
    ):
        if policy.get(field) is not False:
            raise SkillConfigurationError(f"policy.{field} must be false.")
    if not isinstance(servers, list) or not isinstance(bindings, list):
        raise SkillConfigurationError("MCP servers and capability bindings are required.")
    server_index: dict[str, dict[str, Any]] = {}
    for server in servers:
        if not isinstance(server, dict):
            raise SkillConfigurationError("MCP server entries must be objects.")
        server_id = server.get("id")
        if server_id in server_index or server_id not in REQUIRED_MCP_SERVERS:
            raise SkillConfigurationError(f"Invalid or duplicate MCP server: {server_id!r}")
        if server.get("enabled") is not True or server.get("required") is not True:
            raise SkillConfigurationError(f"MCP server '{server_id}' must be enabled and required.")
        tools = _string_list(server.get("tool_allowlist"), f"{server_id}.tool_allowlist")
        server_index[server_id] = {**server, "tool_allowlist": tools}
    if set(server_index) != REQUIRED_MCP_SERVERS:
        raise SkillConfigurationError("Required MCP server definitions are incomplete.")
    if "initial_instructions" not in server_index["serena"]["tool_allowlist"]:
        raise SkillConfigurationError("Serena allowlist must include initial_instructions.")

    capability_bindings: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            raise SkillConfigurationError("MCP capability bindings must be objects.")
        capability_id = binding.get("capability_id")
        if not isinstance(capability_id, str) or capability_id in capability_bindings:
            raise SkillConfigurationError("MCP capability ids must be unique strings.")
        allowed = set(
            _string_list(
                binding.get("allowed_servers"),
                f"{capability_id}.allowed_servers",
            )
        )
        if not allowed <= set(server_index):
            raise SkillConfigurationError(f"Unknown MCP server for capability '{capability_id}'.")
        for server_id in allowed:
            tool_key = f"{server_id}_tools"
            selected_tools = set(
                _string_list(binding.get(tool_key), tool_key, allow_empty=True)
            )
            if not selected_tools <= set(server_index[server_id]["tool_allowlist"]):
                raise SkillConfigurationError(
                    f"Capability '{capability_id}' selects unauthorized {server_id} tools."
                )
        capability_bindings[capability_id] = {
            **binding,
            "allowed_servers": tuple(sorted(allowed)),
        }
    if capability_ids is not None and set(capability_bindings) != capability_ids:
        raise SkillConfigurationError(
            "MCP capability bindings must exactly match logical capabilities."
        )

    if not isinstance(required_use, list) or not required_use:
        raise SkillConfigurationError("MCP required-use bindings are required.")
    required_use_index: dict[str, dict[str, Any]] = {}
    for binding in required_use:
        if not isinstance(binding, dict):
            raise SkillConfigurationError("MCP required-use bindings must be objects.")
        binding_id = binding.get("id")
        if not isinstance(binding_id, str) or binding_id in required_use_index:
            raise SkillConfigurationError("MCP required-use binding ids must be unique.")
        if binding.get("no_fallback") is not True:
            raise SkillConfigurationError(f"MCP binding '{binding_id}' must have no fallback.")
        server_ids = set(
            _string_list(binding.get("server_ids"), f"{binding_id}.server_ids")
        )
        if not server_ids <= set(server_index):
            raise SkillConfigurationError(f"MCP binding '{binding_id}' uses an unknown server.")
        tool_ids = set(
            _string_list(
                binding.get("tool_ids"),
                f"{binding_id}.tool_ids",
                allow_empty=True,
            )
        )
        allowed_tools = {
            tool
            for server_id in server_ids
            for tool in server_index[server_id]["tool_allowlist"]
        }
        if not tool_ids <= allowed_tools:
            raise SkillConfigurationError(f"MCP binding '{binding_id}' uses an unknown tool.")
        required_use_index[binding_id] = binding
    return {
        "policy": dict(policy),
        "servers": server_index,
        "capability_bindings": capability_bindings,
        "required_use_bindings": required_use_index,
    }


def _validate_skill_package(
    entry: Mapping[str, Any],
    source_root: Path,
) -> tuple[Path, str]:
    skill_id = str(entry["id"])
    relative_path = entry.get("path")
    if not isinstance(relative_path, str):
        raise SkillConfigurationError(f"Skill '{skill_id}' must declare path.")
    skill_md = _inside_root(source_root, relative_path, f"{skill_id}.path")
    if skill_md.name != "SKILL.md" or skill_md.parent.name != skill_id:
        raise SkillConfigurationError(
            f"Skill '{skill_id}' path must be {skill_id}/SKILL.md."
        )
    metadata = skill_md.parent / "agents" / "openai.yaml"
    if not skill_md.is_file():
        raise SkillConfigurationError(f"Missing SKILL.md for {skill_id}")
    if not metadata.is_file():
        raise SkillConfigurationError(f"Missing agents/openai.yaml for {skill_id}")
    if (skill_md.parent / "references").exists():
        raise SkillConfigurationError(
            f"References directories are not allowed: {skill_md.parent / 'references'}"
        )

    skill_bytes = skill_md.read_bytes()
    observed_digest = hashlib.sha256(skill_bytes).hexdigest()
    if entry.get("sha256") != observed_digest:
        raise SkillConfigurationError(
            f"Skill '{skill_id}' digest mismatch: expected {entry.get('sha256')}, "
            f"observed {observed_digest}"
        )
    skill_text = _read_utf8(skill_md)
    metadata_text = _read_utf8(metadata)
    _validate_english_text(skill_md, skill_text)
    _validate_english_text(metadata, metadata_text)
    budget = entry.get("content_budget_chars")
    if not isinstance(budget, int) or budget < 1:
        raise SkillConfigurationError(
            f"Skill '{skill_id}' content_budget_chars must be positive."
        )
    if len(skill_text) > budget:
        raise SkillConfigurationError(
            f"Skill '{skill_id}' exceeds content_budget_chars={budget}."
        )

    frontmatter_match = FRONTMATTER_PATTERN.match(skill_text)
    if not frontmatter_match:
        raise SkillConfigurationError(f"Invalid frontmatter in {skill_md}")
    frontmatter = frontmatter_match.group("body")
    name_match = NAME_PATTERN.search(frontmatter)
    description_match = DESCRIPTION_PATTERN.search(frontmatter)
    if not name_match or name_match.group("name") != skill_id:
        raise SkillConfigurationError(
            f"Frontmatter name must match catalog id '{skill_id}' in {skill_md}"
        )
    if not description_match or not description_match.group("description").strip():
        raise SkillConfigurationError(f"Missing skill description in {skill_md}")
    if f"Use ${skill_id} " not in metadata_text:
        raise SkillConfigurationError(
            f"Default prompt must explicitly invoke ${skill_id} in {metadata}"
        )
    short_description_match = SHORT_DESCRIPTION_PATTERN.search(metadata_text)
    if not short_description_match:
        raise SkillConfigurationError(f"Missing short_description in {metadata}")
    if not 25 <= len(short_description_match.group("description")) <= 64:
        raise SkillConfigurationError(
            f"short_description must contain 25 to 64 characters in {metadata}"
        )
    if "allow_implicit_invocation: false" not in metadata_text:
        raise SkillConfigurationError(f"Implicit invocation must be disabled in {metadata}")
    return skill_md, skill_text


def validate_catalog(
    data: dict[str, Any],
    *,
    source_root: Path | None = None,
    mcp_policy: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    settings = data.get("catalog")
    if not isinstance(settings, dict) or settings.get("version") != 2:
        raise SkillConfigurationError("Skill catalog version must be 2.")
    if settings.get("scope") != "project":
        raise SkillConfigurationError("Catalog scope must be 'project'.")
    if settings.get("explicit_injection_only") is not True:
        raise SkillConfigurationError("Skills must use explicit injection only.")
    if settings.get("digest_algorithm") != "sha256":
        raise SkillConfigurationError("Skill digest_algorithm must be sha256.")
    if settings.get("path_authority") != "catalog-entry":
        raise SkillConfigurationError("Skill path authority must be catalog-entry.")
    if settings.get("max_technology_skills_per_task") != 0:
        raise SkillConfigurationError("Active technology Skills are retired.")
    max_skills = settings.get("max_skills_per_task")
    if not isinstance(max_skills, int) or max_skills < 1:
        raise SkillConfigurationError("max_skills_per_task must be positive.")
    professional_skill_id = settings.get("professional_skill_id")
    if professional_skill_id != "professional-profile-runtime":
        raise SkillConfigurationError(
            "professional_skill_id must be 'professional-profile-runtime'."
        )
    capabilities = _string_list(settings.get("capabilities"), "catalog.capabilities")
    if any(not CAPABILITY_PATTERN.fullmatch(item) for item in capabilities):
        raise SkillConfigurationError("Catalog capabilities contain invalid ids.")
    forbidden = set(
        _string_list(
            settings.get("forbidden_expansions"),
            "catalog.forbidden_expansions",
        )
    )
    if forbidden != REQUIRED_FORBIDDEN_EXPANSIONS:
        raise SkillConfigurationError("Catalog forbidden_expansions is incomplete.")
    policy = validate_mcp_policy(
        mcp_policy or load_mcp_policy(),
        set(capabilities),
    )
    known_servers = set(policy["servers"])
    root = Path(source_root or LAYOUT.skill_root).resolve()

    entries = data.get("skills")
    if not isinstance(entries, list) or not entries:
        raise SkillConfigurationError("Catalog must contain Skills.")
    index: dict[str, dict[str, Any]] = {}
    package_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SkillConfigurationError("Skill entries must be objects.")
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            raise SkillConfigurationError(
                f"Skill entry contains authority or unsupported fields: {sorted(unknown)}"
            )
        skill_id = entry.get("id")
        if not isinstance(skill_id, str) or not SKILL_ID_PATTERN.fullmatch(skill_id):
            raise SkillConfigurationError(f"Invalid skill id: {skill_id!r}")
        if skill_id in index:
            raise SkillConfigurationError(f"Duplicate skill id: {skill_id}")
        if not isinstance(entry.get("version"), str) or not VERSION_PATTERN.fullmatch(
            entry["version"]
        ):
            raise SkillConfigurationError(f"Skill '{skill_id}' has invalid version.")
        if not isinstance(entry.get("sha256"), str) or not SHA256_PATTERN.fullmatch(
            entry["sha256"]
        ):
            raise SkillConfigurationError(f"Skill '{skill_id}' has invalid SHA-256.")
        if entry.get("kind") not in ALLOWED_KINDS:
            raise SkillConfigurationError(
                f"Skill '{skill_id}' has invalid kind: {entry.get('kind')!r}"
            )
        eligible = _string_list(
            entry.get("eligible_capabilities"),
            f"{skill_id}.eligible_capabilities",
        )
        if not set(eligible) <= set(capabilities):
            raise SkillConfigurationError(
                f"Skill '{skill_id}' has invalid eligible capabilities."
            )
        prerequisites = _string_list(
            entry.get("mcp_prerequisites"),
            f"{skill_id}.mcp_prerequisites",
            allow_empty=True,
        )
        if not set(prerequisites) <= known_servers:
            raise SkillConfigurationError(
                f"Skill '{skill_id}' declares an unauthorized MCP prerequisite."
            )
        if not isinstance(entry.get("summary"), str) or not entry["summary"].strip():
            raise SkillConfigurationError(f"Skill '{skill_id}' has no summary.")
        skill_md, _ = _validate_skill_package(entry, root)
        package_ids.add(skill_md.parent.name)
        index[skill_id] = {
            **entry,
            "eligible_capabilities": list(eligible),
            "mcp_prerequisites": list(prerequisites),
        }

    professional_entries = [
        skill_id
        for skill_id, entry in index.items()
        if entry["kind"] == "professional"
    ]
    if professional_entries != [professional_skill_id]:
        raise SkillConfigurationError(
            "The catalog must contain exactly one professional runtime Skill."
        )
    source_skill_dirs = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    if source_skill_dirs != package_ids:
        missing = sorted(package_ids - source_skill_dirs)
        unlisted = sorted(source_skill_dirs - package_ids)
        raise SkillConfigurationError(
            f"Catalog/package mismatch. Missing={missing}, unlisted={unlisted}"
        )
    return index


def synchronize(data: dict[str, Any], index: dict[str, dict[str, Any]]) -> None:
    del index
    validate_catalog(data)


def resolve_selection(
    data: dict[str, Any],
    index: dict[str, dict[str, Any]],
    capability: str,
    skill_ids: list[str],
    *,
    transition_id: str | None = None,
    authorized_mcp_servers: set[str] | None = None,
    mcp_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    settings = data["catalog"]
    if capability not in settings["capabilities"]:
        raise SkillConfigurationError(f"Unknown capability: {capability}")
    if len(skill_ids) != len(set(skill_ids)):
        raise SkillConfigurationError("A Skill may be selected only once.")
    professional_skill_id = settings["professional_skill_id"]
    requested = list(skill_ids)
    if professional_skill_id not in requested:
        requested.insert(0, professional_skill_id)
    if len(requested) > settings["max_skills_per_task"]:
        raise SkillConfigurationError(
            f"Selection exceeds max_skills_per_task={settings['max_skills_per_task']}."
        )
    policy = validate_mcp_policy(
        mcp_policy or load_mcp_policy(),
        set(settings["capabilities"]),
    )
    capability_servers = set(
        policy["capability_bindings"][capability]["allowed_servers"]
    )
    if authorized_mcp_servers is not None:
        capability_servers &= set(authorized_mcp_servers)

    selected: list[dict[str, Any]] = []
    professional_count = 0
    for skill_id in requested:
        entry = index.get(skill_id)
        if entry is None:
            raise SkillConfigurationError(f"Unknown Skill: {skill_id}")
        if capability not in entry["eligible_capabilities"]:
            raise SkillConfigurationError(
                f"Capability '{capability}' is not eligible for Skill '{skill_id}'."
            )
        prerequisites = set(entry["mcp_prerequisites"])
        if not prerequisites <= capability_servers:
            raise SkillConfigurationError(
                f"Skill '{skill_id}' MCP prerequisites exceed the admitted policy."
            )
        if entry["kind"] == "professional":
            professional_count += 1
        selected.append(
            {
                "id": skill_id,
                "version": entry["version"],
                "kind": entry["kind"],
                "path": entry["path"],
                "sha256": entry["sha256"],
                "content_budget_chars": entry["content_budget_chars"],
                "mcp_prerequisites": list(entry["mcp_prerequisites"]),
            }
        )
    if professional_count != 1:
        raise SkillConfigurationError(
            "Every activation must select exactly one professional runtime Skill."
        )
    return {
        "scope": "project",
        "capability": capability,
        "transition_id": transition_id,
        "catalog_revision": settings["revision"],
        "explicit_injection": True,
        "skills": selected,
    }


def list_for_capability(
    data: dict[str, Any],
    index: dict[str, dict[str, Any]],
    capability: str | None,
) -> list[dict[str, Any]]:
    if capability is not None and capability not in data["catalog"]["capabilities"]:
        raise SkillConfigurationError(f"Unknown capability: {capability}")
    result = []
    for skill_id, entry in sorted(index.items()):
        if capability is None or capability in entry["eligible_capabilities"]:
            result.append(
                {
                    "id": skill_id,
                    "version": entry["version"],
                    "kind": entry["kind"],
                    "summary": entry["summary"],
                    "eligible_capabilities": entry["eligible_capabilities"],
                    "sha256": entry["sha256"],
                }
            )
    return result


def list_for_role(
    data: dict[str, Any],
    index: dict[str, dict[str, Any]],
    role: str | None,
) -> list[dict[str, Any]]:
    """Compatibility alias; roles are logical capabilities in catalog v2."""

    return list_for_capability(data, index, role)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage project-local digest-pinned Skills."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the project Skill catalog.")
    subparsers.add_parser("sync", help="Validate canonical Skills without a mirror.")
    list_parser = subparsers.add_parser("list", help="List catalog Skills.")
    list_parser.add_argument("--capability")
    list_parser.add_argument("--role", help=argparse.SUPPRESS)
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Emit an explicit digest-pinned Skill packet.",
    )
    resolve_parser.add_argument("--capability")
    resolve_parser.add_argument("--role", help=argparse.SUPPRESS)
    resolve_parser.add_argument("--transition")
    resolve_parser.add_argument("--skill", action="append", dest="skills")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        data = load_catalog()
        index = validate_catalog(data)
        capability = getattr(args, "capability", None) or getattr(args, "role", None)
        if args.command == "validate":
            print(
                f"Validated {len(index)} project-local Skills for "
                f"{len(data['catalog']['capabilities'])} logical capabilities."
            )
        elif args.command == "sync":
            synchronize(data, index)
            print(f"Validated {len(index)} canonical project-local Skills.")
        elif args.command == "list":
            print(json.dumps(list_for_capability(data, index, capability), indent=2))
        elif args.command == "resolve":
            if capability is None:
                raise SkillConfigurationError("--capability is required.")
            packet = resolve_selection(
                data,
                index,
                capability,
                args.skills or [],
                transition_id=args.transition,
            )
            print(json.dumps(packet, indent=2))
        else:
            parser.error(f"Unknown command: {args.command}")
        return 0
    except SkillConfigurationError as exc:
        print(f"Skill configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
