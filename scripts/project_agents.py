#!/usr/bin/env python3
"""Validate, compile, synchronize, and resolve project-local agent seats."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import secrets
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from scripts.project_skills import (
        SkillConfigurationError,
        load_catalog,
        resolve_selection,
        validate_catalog,
    )
except ModuleNotFoundError:
    from project_skills import (  # type: ignore[no-redef]
        SkillConfigurationError,
        load_catalog,
        resolve_selection,
        validate_catalog,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEAM_PATH = PROJECT_ROOT / "agents" / "team.toml"
EXPECTED_ROLE_TEMPLATES = {
    "pm",
    "pl",
    "ta",
    "developer_slot",
    "qa_sdet",
    "build_release",
}
ROLE_PREFIXES = {
    "pm": "PM",
    "pl": "PL",
    "ta": "TA",
    "dev_1": "DEV",
    "dev_2": "DEV",
    "dev_3": "DEV",
    "qa_sdet": "QA_SDET",
    "build_release": "BUILD_RELEASE",
}
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}
ALLOWED_MODELS = {
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}
HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
KOREAN_NAME_PATTERN = re.compile(r"^[\uac00-\ud7a3]{2,4}$")
KOREAN_FAMILY_NAME_PATTERN = re.compile(r"^[\uac00-\ud7a3]{1,2}$")
KOREAN_GIVEN_NAME_PATTERN = re.compile(r"^[\uac00-\ud7a3]{2,3}$")
SEAT_ID_PATTERN = re.compile(
    r"^(?P<prefix>[A-Z][A-Z0-9_]*?)_(?P<display_name>[\uac00-\ud7a3]{2,4})$"
)
ASCII_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MESSAGE_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
GLOBAL_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]|~[/\\]|CODEX_HOME|CODEXHOME)",
    re.IGNORECASE,
)


class AgentConfigurationError(RuntimeError):
    """Raised when project-local agent configuration is invalid."""


def _inside_project(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise AgentConfigurationError(
            f"Path escapes the project root: {resolved}"
        ) from exc
    return resolved


def _read_toml(path: Path) -> dict[str, Any]:
    path = _inside_project(path)
    if not path.is_file():
        raise AgentConfigurationError(f"Configuration file not found: {path}")
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise AgentConfigurationError(f"Invalid TOML in {path}: {exc}") from exc


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AgentConfigurationError(f"File is not UTF-8: {path}") from exc


def _configured_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentConfigurationError(f"{label} must be a non-empty relative path.")
    path = Path(value)
    if path.is_absolute():
        raise AgentConfigurationError(f"{label} must be project-relative: {value}")
    return _inside_project(PROJECT_ROOT / path)


def _require_english(path: Path, label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentConfigurationError(f"{label} must be non-empty in {path}.")
    if HANGUL_PATTERN.search(value):
        raise AgentConfigurationError(
            f"Implementation prose must be English in {path}: {label}"
        )
    if "TODO" in value:
        raise AgentConfigurationError(f"Unresolved TODO marker in {path}: {label}")
    return value


def _require_ascii_id(path: Path, label: str, value: Any) -> str:
    value = _require_english(path, label, value)
    if not ASCII_ID_PATTERN.fullmatch(value):
        raise AgentConfigurationError(f"Invalid {label} in {path}: {value!r}")
    return value


def _require_string_list(
    path: Path,
    label: str,
    value: Any,
    pattern: re.Pattern[str] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise AgentConfigurationError(f"{label} must be a string list in {path}.")
    if any(not isinstance(item, str) or not item for item in value):
        raise AgentConfigurationError(f"{label} contains an invalid value in {path}.")
    if len(value) != len(set(value)):
        raise AgentConfigurationError(f"{label} contains duplicates in {path}.")
    if pattern is not None and any(not pattern.fullmatch(item) for item in value):
        raise AgentConfigurationError(f"{label} contains an invalid identifier in {path}.")
    return value


def _scan_source_policy(paths: list[Path]) -> None:
    for path in paths:
        text = _read_utf8(path)
        if "TODO" in text:
            raise AgentConfigurationError(f"Unresolved TODO marker in {path}.")
        if GLOBAL_PATH_PATTERN.search(text):
            raise AgentConfigurationError(
                f"Global or absolute path dependency is not allowed in {path}."
            )
    agents_root = PROJECT_ROOT / "agents"
    references_dirs = [
        path for path in agents_root.rglob("*") if path.is_dir() and path.name == "references"
    ]
    if references_dirs:
        raise AgentConfigurationError(
            f"References directories are not allowed: {references_dirs[0]}"
        )


def _load_profiles(path: Path) -> dict[str, dict[str, str]]:
    data = _read_toml(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise AgentConfigurationError(f"Missing [profiles] tables in {path}.")

    result: dict[str, dict[str, str]] = {}
    for profile_id, profile in profiles.items():
        _require_ascii_id(path, "profile id", profile_id)
        if not isinstance(profile, dict):
            raise AgentConfigurationError(f"Invalid profile '{profile_id}' in {path}.")
        unknown_keys = set(profile) - {
            "model",
            "model_reasoning_effort",
            "sandbox_mode",
        }
        if unknown_keys:
            raise AgentConfigurationError(
                f"Unsupported profile fields for '{profile_id}': {sorted(unknown_keys)}"
            )
        model = profile.get("model")
        effort = profile.get("model_reasoning_effort")
        sandbox_mode = profile.get("sandbox_mode")
        if model not in ALLOWED_MODELS:
            raise AgentConfigurationError(
                f"Invalid GPT-5.6 model for '{profile_id}': {model!r}"
            )
        if effort not in ALLOWED_REASONING_EFFORTS:
            raise AgentConfigurationError(
                f"Invalid model_reasoning_effort for '{profile_id}': {effort!r}"
            )
        if sandbox_mode not in ALLOWED_SANDBOX_MODES:
            raise AgentConfigurationError(
                f"Invalid sandbox_mode for '{profile_id}': {sandbox_mode!r}"
            )
        result[profile_id] = {
            "model": model,
            "model_reasoning_effort": effort,
            "sandbox_mode": sandbox_mode,
        }
    return result


def _load_roles(paths: list[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    seen_role_keys: set[str] = set()
    for path in paths:
        data = _read_toml(path)
        role = data.get("role")
        if not isinstance(role, dict):
            raise AgentConfigurationError(f"Missing [role] table in {path}.")
        role_id = _require_ascii_id(path, "role id", role.get("id"))
        if role_id in result:
            raise AgentConfigurationError(f"Duplicate role template id: {role_id}")
        if path.stem.replace("-", "_") != role_id:
            raise AgentConfigurationError(
                f"Role filename must match role id '{role_id}': {path.name}"
            )
        title = _require_english(path, "title", role.get("title"))
        instructions = _require_english(path, "instructions", role.get("instructions"))
        role_keys = _require_string_list(
            path,
            "role_keys",
            role.get("role_keys"),
            ASCII_ID_PATTERN,
        )
        overlapping_keys = seen_role_keys.intersection(role_keys)
        if overlapping_keys:
            raise AgentConfigurationError(
                f"Role keys appear in multiple templates: {sorted(overlapping_keys)}"
            )
        seen_role_keys.update(role_keys)
        authorities = _require_string_list(
            path,
            "approval_authorities",
            role.get("approval_authorities"),
            ASCII_ID_PATTERN,
            allow_empty=True,
        )
        accepted = _require_string_list(
            path,
            "accepted_message_types",
            role.get("accepted_message_types"),
            MESSAGE_TYPE_PATTERN,
        )
        emitted = _require_string_list(
            path,
            "emitted_message_types",
            role.get("emitted_message_types"),
            MESSAGE_TYPE_PATTERN,
        )
        result[role_id] = {
            "id": role_id,
            "title": title,
            "instructions": instructions.strip(),
            "role_keys": role_keys,
            "approval_authorities": authorities,
            "accepted_message_types": accepted,
            "emitted_message_types": emitted,
            "path": path,
        }
    if set(result) != EXPECTED_ROLE_TEMPLATES:
        raise AgentConfigurationError(
            "Role template set mismatch. "
            f"Expected={sorted(EXPECTED_ROLE_TEMPLATES)}, actual={sorted(result)}"
        )
    return result


def _load_slots(
    path: Path,
    roles: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    data = _read_toml(path)
    slot_entries = data.get("slots")
    if not isinstance(slot_entries, list) or not slot_entries:
        raise AgentConfigurationError(f"Missing [[slots]] entries in {path}.")
    result: dict[str, dict[str, Any]] = {}
    seen_role_keys: set[str] = set()
    seen_prefix_role_pairs: set[tuple[str, str]] = set()

    for slot in slot_entries:
        if not isinstance(slot, dict):
            raise AgentConfigurationError(f"Invalid [[slots]] entry in {path}.")
        slot_key = _require_ascii_id(path, "slot_key", slot.get("slot_key"))
        role_key = _require_ascii_id(path, "role_key", slot.get("role_key"))
        role_template = _require_ascii_id(
            path,
            "role_template",
            slot.get("role_template"),
        )
        runtime_profile = _require_ascii_id(
            path,
            "runtime_profile",
            slot.get("runtime_profile"),
        )
        id_prefix = slot.get("id_prefix")
        if (
            not isinstance(id_prefix, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", id_prefix)
        ):
            raise AgentConfigurationError(
                f"Invalid id_prefix for slot '{slot_key}' in {path}."
            )
        expected_prefix = ROLE_PREFIXES.get(role_key)
        if expected_prefix is None:
            raise AgentConfigurationError(f"Unknown role_key in {path}: {role_key}")
        if id_prefix != expected_prefix:
            raise AgentConfigurationError(
                f"Seat prefix for '{role_key}' must be '{expected_prefix}' in {path}."
            )
        role = roles.get(role_template)
        if role is None or role_key not in role["role_keys"]:
            raise AgentConfigurationError(
                f"Role template '{role_template}' does not own '{role_key}' in {path}."
            )
        if runtime_profile not in profiles:
            raise AgentConfigurationError(
                f"Unknown runtime_profile '{runtime_profile}' in {path}."
            )
        if slot_key in result:
            raise AgentConfigurationError(f"Duplicate slot_key: {slot_key}")
        if role_key in seen_role_keys:
            raise AgentConfigurationError(f"Duplicate role_key assignment: {role_key}")
        prefix_role_pair = (id_prefix, role_key)
        if prefix_role_pair in seen_prefix_role_pairs:
            raise AgentConfigurationError(
                f"Duplicate prefix and role pair for slot '{slot_key}'."
            )

        seen_role_keys.add(role_key)
        seen_prefix_role_pairs.add(prefix_role_pair)
        result[slot_key] = {
            "slot_key": slot_key,
            "id_prefix": id_prefix,
            "role_key": role_key,
            "role_template": role_template,
            "runtime_profile": runtime_profile,
        }
    return result


def _load_name_pool(path: Path) -> dict[str, list[str]]:
    data = _read_toml(path)
    pool = data.get("name_pool")
    if not isinstance(pool, dict):
        raise AgentConfigurationError(f"Missing [name_pool] table in {path}.")
    family_names = _require_string_list(
        path,
        "family_names",
        pool.get("family_names"),
    )
    given_names = _require_string_list(
        path,
        "given_names",
        pool.get("given_names"),
    )
    if any(not KOREAN_FAMILY_NAME_PATTERN.fullmatch(name) for name in family_names):
        raise AgentConfigurationError(
            f"Family names must contain one or two Korean characters in {path}."
        )
    if any(not KOREAN_GIVEN_NAME_PATTERN.fullmatch(name) for name in given_names):
        raise AgentConfigurationError(
            f"Given names must contain two or three Korean characters in {path}."
        )
    return {
        "family_names": family_names,
        "given_names": given_names,
    }


def _load_seat_registry(
    path: Path,
    slots: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    data = _read_toml(path)
    registry = data.get("registry")
    seat_entries = data.get("seats")
    if not isinstance(registry, dict) or registry.get("version") != 1:
        raise AgentConfigurationError(f"Invalid [registry] table in {path}.")
    if registry.get("generator") != "scripts/project_agents.py":
        raise AgentConfigurationError(f"Invalid registry generator in {path}.")
    if not isinstance(registry.get("generated_at_utc"), str):
        raise AgentConfigurationError(f"Missing generated_at_utc in {path}.")
    if not isinstance(seat_entries, list) or not seat_entries:
        raise AgentConfigurationError(f"Missing [[seats]] entries in {path}.")

    result: dict[str, dict[str, Any]] = {}
    seen_slot_keys: set[str] = set()
    seen_display_names: set[str] = set()
    for entry in seat_entries:
        if not isinstance(entry, dict):
            raise AgentConfigurationError(f"Invalid [[seats]] entry in {path}.")
        slot_key = _require_ascii_id(path, "slot_key", entry.get("slot_key"))
        slot = slots.get(slot_key)
        if slot is None:
            raise AgentConfigurationError(
                f"Unknown slot_key '{slot_key}' in {path}."
            )
        seat_id = entry.get("seat_id")
        display_name = entry.get("display_name")
        if not isinstance(seat_id, str):
            raise AgentConfigurationError(f"seat_id must be a string in {path}.")
        match = SEAT_ID_PATTERN.fullmatch(seat_id)
        if not match:
            raise AgentConfigurationError(
                f"seat_id must use ROLE_KoreanName format in {path}: {seat_id!r}"
            )
        if match.group("prefix") != slot["id_prefix"]:
            raise AgentConfigurationError(
                f"seat_id prefix does not match slot '{slot_key}' in {path}."
            )
        if (
            not isinstance(display_name, str)
            or not KOREAN_NAME_PATTERN.fullmatch(display_name)
        ):
            raise AgentConfigurationError(
                f"display_name must be a Korean name in {path}."
            )
        if display_name != match.group("display_name"):
            raise AgentConfigurationError(
                f"display_name must match the seat_id suffix in {path}."
            )
        if seat_id in result:
            raise AgentConfigurationError(f"Duplicate seat_id: {seat_id}")
        if slot_key in seen_slot_keys:
            raise AgentConfigurationError(f"Duplicate slot_key: {slot_key}")
        if display_name in seen_display_names:
            raise AgentConfigurationError(
                f"Duplicate Korean display_name: {display_name}"
            )
        seen_slot_keys.add(slot_key)
        seen_display_names.add(display_name)
        result[seat_id] = {
            **slot,
            "seat_id": seat_id,
            "display_name": display_name,
            "enabled": True,
            "path": path,
        }
    if seen_slot_keys != set(slots):
        raise AgentConfigurationError(
            "Seat registry must contain every configured slot exactly once."
        )
    return result


def _load_static_configuration() -> dict[str, Any]:
    data = _read_toml(TEAM_PATH)
    team = data.get("team")
    if not isinstance(team, dict):
        raise AgentConfigurationError(f"Missing [team] table in {TEAM_PATH}.")
    if team.get("version") != 1:
        raise AgentConfigurationError("Team configuration version must be 1.")
    if team.get("scope") != "project":
        raise AgentConfigurationError("Team scope must be 'project'.")
    if team.get("max_seats") != 8:
        raise AgentConfigurationError("This team must define exactly eight seats.")
    if team.get("message_transport") != "sqlite":
        raise AgentConfigurationError("Message transport must be 'sqlite'.")
    if team.get("model_policy") != "profile-pinned":
        raise AgentConfigurationError("Model policy must be 'profile-pinned'.")

    source_root = _configured_path(team.get("source_root"), "source_root")
    runtime_root = _configured_path(team.get("runtime_root"), "runtime_root")
    skill_catalog_path = _configured_path(
        team.get("skill_catalog"),
        "skill_catalog",
    )
    profiles_path = _configured_path(
        team.get("runtime_profiles"),
        "runtime_profiles",
    )
    slots_path = _configured_path(team.get("seat_slots"), "seat_slots")
    name_pool_path = _configured_path(team.get("name_pool"), "name_pool")
    registry_path = _configured_path(team.get("seat_registry"), "seat_registry")
    serena_knowledge_policy_path = _configured_path(
        team.get("serena_knowledge_policy"), "serena_knowledge_policy"
    )
    serena_service_path = _configured_path(team.get("serena_service"), "serena_service")
    if source_root != (PROJECT_ROOT / "agents").resolve():
        raise AgentConfigurationError("Canonical agent source must be agents/.")
    if runtime_root != (PROJECT_ROOT / ".codex" / "agents").resolve():
        raise AgentConfigurationError("Runtime agent root must be .codex/agents/.")
    if skill_catalog_path != (PROJECT_ROOT / "skills" / "catalog.toml").resolve():
        raise AgentConfigurationError(
            "Agent configuration must use the project skill catalog."
        )

    role_values = team.get("role_files")
    if not isinstance(role_values, list) or not role_values:
        raise AgentConfigurationError("role_files must be a non-empty list.")
    role_paths = [_configured_path(value, "role_files entry") for value in role_values]
    if len(role_paths) != len(set(role_paths)):
        raise AgentConfigurationError("role_files contains duplicates.")

    base_instructions = _require_english(
        TEAM_PATH,
        "base_instructions",
        team.get("base_instructions"),
    ).strip()
    required_placeholders = {"{{seat_id}}", "{{role_key}}", "{{role_title}}"}
    missing_placeholders = [
        marker for marker in required_placeholders if marker not in base_instructions
    ]
    if missing_placeholders:
        raise AgentConfigurationError(
            f"base_instructions is missing placeholders: {missing_placeholders}"
        )

    profiles = _load_profiles(profiles_path)
    roles = _load_roles(role_paths)
    slots = _load_slots(slots_path, roles, profiles)
    if len(slots) != team["max_seats"]:
        raise AgentConfigurationError(
            f"Expected {team['max_seats']} slots, found {len(slots)}."
        )
    name_pool = _load_name_pool(name_pool_path)

    skill_catalog = load_catalog()
    skill_index = validate_catalog(skill_catalog)
    catalog_roles = set(skill_catalog["catalog"]["roles"])
    slot_role_keys = {slot["role_key"] for slot in slots.values()}
    if slot_role_keys != catalog_roles:
        raise AgentConfigurationError(
            "Slot role keys must exactly match the skill catalog roles. "
            f"Missing={sorted(catalog_roles - slot_role_keys)}, "
            f"extra={sorted(slot_role_keys - catalog_roles)}"
        )

    source_files = [
        TEAM_PATH,
        profiles_path,
        slots_path,
        name_pool_path,
        serena_knowledge_policy_path,
        serena_service_path,
        *role_paths,
    ]
    _scan_source_policy(source_files)
    return {
        "team": team,
        "base_instructions": base_instructions,
        "source_root": source_root,
        "runtime_root": runtime_root,
        "registry_path": registry_path,
        "serena_knowledge_policy_path": serena_knowledge_policy_path,
        "serena_service_path": serena_service_path,
        "profiles": profiles,
        "roles": roles,
        "slots": slots,
        "name_pool": name_pool,
        "skill_catalog": skill_catalog,
        "skill_index": skill_index,
    }


def load_and_validate() -> dict[str, Any]:
    bundle = _load_static_configuration()
    registry_path: Path = bundle["registry_path"]
    if not registry_path.is_file():
        raise AgentConfigurationError(
            "Seat registry is not initialized. Run "
            "'python scripts/project_agents.py init'."
        )
    seats = _load_seat_registry(registry_path, bundle["slots"])
    if len(seats) != bundle["team"]["max_seats"]:
        raise AgentConfigurationError(
            f"Expected {bundle['team']['max_seats']} seats, found {len(seats)}."
        )
    _scan_source_policy([registry_path])
    return {**bundle, "seats": seats}


def generate_unique_display_names(
    name_pool: dict[str, list[str]],
    count: int,
    rng: Any | None = None,
) -> list[str]:
    combinations = [
        f"{family}{given}"
        for family in name_pool["family_names"]
        for given in name_pool["given_names"]
    ]
    if len(combinations) < count:
        raise AgentConfigurationError(
            f"Name pool can produce only {len(combinations)} unique names."
        )
    random_source = rng if rng is not None else secrets.SystemRandom()
    return random_source.sample(combinations, count)


def _render_registry(
    slots: dict[str, dict[str, Any]],
    display_names: list[str],
) -> str:
    if len(slots) != len(display_names):
        raise AgentConfigurationError("Slot and generated-name counts differ.")
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "[registry]",
        "version = 1",
        'generator = "scripts/project_agents.py"',
        f"generated_at_utc = {_toml_string(generated_at)}",
        "",
    ]
    for slot, display_name in zip(slots.values(), display_names, strict=True):
        seat_id = f"{slot['id_prefix']}_{display_name}"
        lines.extend(
            [
                "[[seats]]",
                f"slot_key = {_toml_string(slot['slot_key'])}",
                f"seat_id = {_toml_string(seat_id)}",
                f"display_name = {_toml_string(display_name)}",
                "",
            ]
        )
    return "\n".join(lines)


def initialize_seats(
    regenerate: bool = False,
    confirm_identity_reset: bool = False,
    rng: Any | None = None,
) -> tuple[dict[str, Any], bool]:
    bundle = _load_static_configuration()
    registry_path: Path = bundle["registry_path"]
    if registry_path.exists() and not regenerate:
        return load_and_validate(), False
    if regenerate and not confirm_identity_reset:
        raise AgentConfigurationError(
            "Regeneration changes durable routing identities. "
            "Pass --confirm-identity-reset explicitly."
        )
    display_names = generate_unique_display_names(
        bundle["name_pool"],
        len(bundle["slots"]),
        rng,
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _inside_project(registry_path.with_suffix(".toml.tmp"))
    temporary_path.write_text(
        _render_registry(bundle["slots"], display_names),
        encoding="utf-8",
        newline="\n",
    )
    temporary_path.replace(registry_path)
    return load_and_validate(), True


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def compile_runtime_agent(bundle: dict[str, Any], seat_id: str) -> str:
    seat = bundle["seats"].get(seat_id)
    if seat is None:
        raise AgentConfigurationError(f"Unknown seat: {seat_id}")
    role = bundle["roles"][seat["role_template"]]
    profile = bundle["profiles"][seat["runtime_profile"]]
    base = (
        bundle["base_instructions"]
        .replace("{{seat_id}}", seat["seat_id"])
        .replace("{{role_key}}", seat["role_key"])
        .replace("{{role_title}}", role["title"])
    )
    authority = (
        ", ".join(role["approval_authorities"])
        if role["approval_authorities"]
        else "none"
    )
    instructions = (
        f"{base}\n\n"
        "Organizational role contract:\n"
        f"{role['instructions']}\n\n"
        f"Approval authority: {authority}.\n"
        "Accepted durable message types: "
        f"{', '.join(role['accepted_message_types'])}.\n"
        "Emitted durable message types: "
        f"{', '.join(role['emitted_message_types'])}."
    )
    if "{{" in instructions or "}}" in instructions:
        raise AgentConfigurationError(
            f"Unresolved template marker while compiling {seat_id}."
        )
    if '"""' in instructions:
        raise AgentConfigurationError(
            f"Triple quotes are not allowed in compiled instructions for {seat_id}."
        )
    description = (
        f"Project-local {role['title']} seat. "
        "Use for the bounded organizational responsibilities defined by this seat."
    )
    return (
        f"name = {_toml_string(seat['seat_id'])}\n"
        f"description = {_toml_string(description)}\n"
        f"model = {_toml_string(profile['model'])}\n"
        "model_reasoning_effort = "
        f"{_toml_string(profile['model_reasoning_effort'])}\n"
        f"sandbox_mode = {_toml_string(profile['sandbox_mode'])}\n"
        'developer_instructions = """\n'
        f"{instructions.strip()}\n"
        '"""\n'
    )


def synchronize(bundle: dict[str, Any]) -> None:
    runtime_root: Path = bundle["runtime_root"]
    runtime_root.mkdir(parents=True, exist_ok=True)
    expected_paths: set[Path] = set()
    for seat_id in sorted(bundle["seats"]):
        target = _inside_project(runtime_root / f"{seat_id}.toml")
        expected_paths.add(target)
        target.write_text(
            compile_runtime_agent(bundle, seat_id),
            encoding="utf-8",
            newline="\n",
        )

    actual_paths = {path.resolve() for path in runtime_root.glob("*.toml")}
    unexpected = actual_paths - expected_paths
    for stale_path in sorted(unexpected):
        _inside_project(stale_path).unlink()

    for target in expected_paths:
        seat_id = target.stem
        if target.read_text(encoding="utf-8") != compile_runtime_agent(
            bundle,
            seat_id,
        ):
            raise AgentConfigurationError(f"Runtime mirror mismatch: {target}")
        parsed = _read_toml(target)
        required = {"name", "description", "developer_instructions"}
        if not required <= set(parsed):
            raise AgentConfigurationError(
                f"Runtime agent is missing required Codex fields: {target}"
            )
        if parsed.get("model") != bundle["profiles"][
            bundle["seats"][seat_id]["runtime_profile"]
        ]["model"]:
            raise AgentConfigurationError(
                f"Runtime agent model does not match its pinned profile: {target}"
            )


def list_seats(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for seat_id, seat in bundle["seats"].items():
        role = bundle["roles"][seat["role_template"]]
        profile = bundle["profiles"][seat["runtime_profile"]]
        result.append(
            {
                "seat_id": seat_id,
                "display_name": seat["display_name"],
                "role_key": seat["role_key"],
                "organizational_role": role["title"],
                "role_template": seat["role_template"],
                "runtime_profile": seat["runtime_profile"],
                "model_policy": bundle["team"]["model_policy"],
                "model": profile["model"],
                "model_reasoning_effort": profile["model_reasoning_effort"],
                "sandbox_mode": profile["sandbox_mode"],
                "runtime_agent": f".codex/agents/{seat_id}.toml",
            }
        )
    return result


def resolve_binding(
    bundle: dict[str, Any],
    seat_id: str,
    skill_ids: list[str],
) -> dict[str, Any]:
    seat = bundle["seats"].get(seat_id)
    if seat is None:
        raise AgentConfigurationError(f"Unknown seat: {seat_id}")
    try:
        skill_packet = resolve_selection(
            bundle["skill_catalog"],
            bundle["skill_index"],
            seat["role_key"],
            skill_ids,
        )
    except SkillConfigurationError as exc:
        raise AgentConfigurationError(str(exc)) from exc
    role = bundle["roles"][seat["role_template"]]
    return {
        "scope": "project",
        "seat_id": seat_id,
        "role_key": seat["role_key"],
        "organizational_role": role["title"],
        "agent_file": f".codex/agents/{seat_id}.toml",
        "model_policy": bundle["team"]["model_policy"],
        "model": bundle["profiles"][seat["runtime_profile"]]["model"],
        "skill_packet": skill_packet,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage project-local organizational agent seats."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "init",
        help="Randomly generate and persist Korean names for uninitialized seats.",
    )
    regenerate_parser = subparsers.add_parser(
        "regenerate",
        help="Replace every durable seat identity with newly generated names.",
    )
    regenerate_parser.add_argument(
        "--confirm-identity-reset",
        action="store_true",
        help="Acknowledge that queued routing identities may become invalid.",
    )
    subparsers.add_parser("validate", help="Validate canonical agent configuration.")
    subparsers.add_parser(
        "sync",
        help="Compile canonical seats into project-local Codex runtime agents.",
    )
    subparsers.add_parser("list", help="List the eight configured logical seats.")
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve a seat and explicit project-local skill packet.",
    )
    resolve_parser.add_argument("--seat", required=True)
    resolve_parser.add_argument(
        "--skill",
        action="append",
        dest="skills",
        required=True,
        help="Skill id to inject. Repeat for multiple skills.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            bundle, created = initialize_seats()
            action = "Generated" if created else "Kept existing"
            print(
                f"{action} {len(bundle['seats'])} durable project-local seat identities."
            )
            print(
                json.dumps(
                    [row["seat_id"] for row in list_seats(bundle)],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "regenerate":
            bundle, _ = initialize_seats(
                regenerate=True,
                confirm_identity_reset=args.confirm_identity_reset,
            )
            print(
                f"Regenerated {len(bundle['seats'])} durable seat identities."
            )
            print(
                json.dumps(
                    [row["seat_id"] for row in list_seats(bundle)],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            bundle = load_and_validate()
        if args.command == "validate":
            print(
                "Validated "
                f"{len(bundle['roles'])} role templates and "
                f"{len(bundle['seats'])} project-local agent seats."
            )
        elif args.command == "sync":
            synchronize(bundle)
            print(
                f"Synchronized {len(bundle['seats'])} project-local Codex agents."
            )
        elif args.command == "list":
            print(
                json.dumps(
                    list_seats(bundle),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "resolve":
            print(
                json.dumps(
                    resolve_binding(bundle, args.seat, args.skills),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command not in {"init", "regenerate"}:
            parser.error(f"Unknown command: {args.command}")
        return 0
    except (AgentConfigurationError, SkillConfigurationError) as exc:
        print(f"Agent configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
