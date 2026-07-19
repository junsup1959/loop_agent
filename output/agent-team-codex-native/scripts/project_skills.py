#!/usr/bin/env python3
"""Validate, synchronize, and resolve project-local expertise skills."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / ".agents" / "skills" / "catalog.toml"
HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")
FRONTMATTER_PATTERN = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
NAME_PATTERN = re.compile(r"^name:\s*(?P<name>[a-z0-9-]+)\s*$", re.MULTILINE)
DESCRIPTION_PATTERN = re.compile(r"^description:\s*(?P<description>.+)\s*$", re.MULTILINE)
SHORT_DESCRIPTION_PATTERN = re.compile(
    r'^  short_description:\s*"(?P<description>[^"]+)"\s*$',
    re.MULTILINE,
)
ALLOWED_KINDS = {"workflow", "technology"}


class SkillConfigurationError(RuntimeError):
    """Raised when the project-local skill configuration is invalid."""


def _inside_project(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise SkillConfigurationError(
            f"Path escapes the project root: {resolved}"
        ) from exc
    return resolved


def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.is_file():
        raise SkillConfigurationError(f"Catalog not found: {CATALOG_PATH}")
    with CATALOG_PATH.open("rb") as stream:
        data = tomllib.load(stream)
    if "catalog" not in data or "skills" not in data:
        raise SkillConfigurationError(
            "Catalog must define [catalog] and at least one [[skills]] entry."
        )
    return data


def _catalog_roots(data: dict[str, Any]) -> tuple[Path, Path]:
    settings = data["catalog"]
    source_root = _inside_project(PROJECT_ROOT / settings["source_root"])
    runtime_root = _inside_project(PROJECT_ROOT / settings["runtime_root"])
    return source_root, runtime_root


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillConfigurationError(f"File is not UTF-8: {path}") from exc


def _validate_english_text(path: Path, text: str) -> None:
    if HANGUL_PATTERN.search(text):
        raise SkillConfigurationError(
            f"Implementation content must be English: {path}"
        )
    if "TODO" in text:
        raise SkillConfigurationError(f"Unresolved TODO marker in {path}")


def _validate_skill_package(skill_id: str, source_root: Path) -> None:
    skill_dir = _inside_project(source_root / skill_id)
    skill_md = skill_dir / "SKILL.md"
    metadata = skill_dir / "agents" / "openai.yaml"

    if not skill_md.is_file():
        raise SkillConfigurationError(f"Missing SKILL.md for {skill_id}")
    if not metadata.is_file():
        raise SkillConfigurationError(f"Missing agents/openai.yaml for {skill_id}")
    if (skill_dir / "references").exists():
        raise SkillConfigurationError(
            f"References directories are not allowed: {skill_dir / 'references'}"
        )

    skill_text = _read_utf8(skill_md)
    metadata_text = _read_utf8(metadata)
    _validate_english_text(skill_md, skill_text)
    _validate_english_text(metadata, metadata_text)

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
    short_description = short_description_match.group("description")
    if not 25 <= len(short_description) <= 64:
        raise SkillConfigurationError(
            f"short_description must contain 25 to 64 characters in {metadata}"
        )
    if "allow_implicit_invocation: false" not in metadata_text:
        raise SkillConfigurationError(
            f"Implicit invocation must be disabled in {metadata}"
        )


def validate_catalog(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    settings = data["catalog"]
    if settings.get("scope") != "project":
        raise SkillConfigurationError("Catalog scope must be 'project'.")
    if settings.get("explicit_injection_only") is not True:
        raise SkillConfigurationError("Skills must use explicit injection only.")

    max_skills = settings.get("max_skills_per_task")
    max_technology = settings.get("max_technology_skills_per_task")
    if not isinstance(max_skills, int) or max_skills < 1:
        raise SkillConfigurationError("max_skills_per_task must be positive.")
    if not isinstance(max_technology, int) or not 0 <= max_technology <= max_skills:
        raise SkillConfigurationError(
            "max_technology_skills_per_task must be between zero and the total limit."
        )

    roles = settings.get("roles", [])
    if not roles or len(roles) != len(set(roles)):
        raise SkillConfigurationError("Catalog roles must be non-empty and unique.")
    allowed_roles = set(roles)
    source_root, _ = _catalog_roots(data)

    index: dict[str, dict[str, Any]] = {}
    for entry in data["skills"]:
        skill_id = entry.get("id")
        if not isinstance(skill_id, str) or not re.fullmatch(r"[a-z0-9-]+", skill_id):
            raise SkillConfigurationError(f"Invalid skill id: {skill_id!r}")
        if skill_id in index:
            raise SkillConfigurationError(f"Duplicate skill id: {skill_id}")
        if entry.get("kind") not in ALLOWED_KINDS:
            raise SkillConfigurationError(
                f"Skill '{skill_id}' has invalid kind: {entry.get('kind')!r}"
            )
        eligible_roles = entry.get("eligible_roles", [])
        if not eligible_roles or not set(eligible_roles) <= allowed_roles:
            raise SkillConfigurationError(
                f"Skill '{skill_id}' has invalid eligible roles."
            )
        if not entry.get("summary"):
            raise SkillConfigurationError(f"Skill '{skill_id}' has no summary.")
        _validate_skill_package(skill_id, source_root)
        index[skill_id] = entry

    source_skill_dirs = {
        path.name
        for path in source_root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    if source_skill_dirs != set(index):
        missing = sorted(set(index) - source_skill_dirs)
        unlisted = sorted(source_skill_dirs - set(index))
        raise SkillConfigurationError(
            f"Catalog/package mismatch. Missing={missing}, unlisted={unlisted}"
        )
    return index


def synchronize(data: dict[str, Any], index: dict[str, dict[str, Any]]) -> None:
    source_root, runtime_root = _catalog_roots(data)
    if source_root != runtime_root:
        raise SkillConfigurationError(
            "Project skills must be read directly from .agents/skills; "
            "runtime mirrors are not supported."
        )
    for skill_id in index:
        _validate_skill_package(skill_id, source_root)


def resolve_selection(
    data: dict[str, Any],
    index: dict[str, dict[str, Any]],
    role: str,
    skill_ids: list[str],
) -> dict[str, Any]:
    settings = data["catalog"]
    if role not in settings["roles"]:
        raise SkillConfigurationError(f"Unknown role: {role}")
    if not skill_ids:
        raise SkillConfigurationError("At least one skill must be selected.")
    if len(skill_ids) != len(set(skill_ids)):
        raise SkillConfigurationError("A skill may be selected only once.")
    if len(skill_ids) > settings["max_skills_per_task"]:
        raise SkillConfigurationError(
            f"Selection exceeds max_skills_per_task={settings['max_skills_per_task']}."
        )

    selected: list[dict[str, Any]] = []
    technology_count = 0
    for skill_id in skill_ids:
        entry = index.get(skill_id)
        if entry is None:
            raise SkillConfigurationError(f"Unknown skill: {skill_id}")
        if role not in entry["eligible_roles"]:
            raise SkillConfigurationError(
                f"Role '{role}' is not eligible for skill '{skill_id}'."
            )
        if entry["kind"] == "technology":
            technology_count += 1
        selected.append(
            {
                "id": skill_id,
                "kind": entry["kind"],
                "skill_md": f".agents/skills/{skill_id}/SKILL.md",
            }
        )

    if technology_count > settings["max_technology_skills_per_task"]:
        raise SkillConfigurationError(
            "Selection exceeds max_technology_skills_per_task="
            f"{settings['max_technology_skills_per_task']}."
        )

    return {
        "scope": "project",
        "role": role,
        "explicit_injection": True,
        "skills": selected,
    }


def list_for_role(
    data: dict[str, Any],
    index: dict[str, dict[str, Any]],
    role: str | None,
) -> list[dict[str, Any]]:
    if role is not None and role not in data["catalog"]["roles"]:
        raise SkillConfigurationError(f"Unknown role: {role}")
    result = []
    for skill_id, entry in sorted(index.items()):
        if role is None or role in entry["eligible_roles"]:
            result.append(
                {
                    "id": skill_id,
                    "kind": entry["kind"],
                    "summary": entry["summary"],
                    "eligible_roles": entry["eligible_roles"],
                }
            )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage project-local expertise skills."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the project skill catalog.")
    subparsers.add_parser(
        "sync",
        help="Validate native project-local skills without creating a runtime mirror.",
    )
    list_parser = subparsers.add_parser(
        "list",
        help="List catalog skills, optionally filtered by organizational role.",
    )
    list_parser.add_argument("--role")
    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Validate and emit an explicit skill injection packet.",
    )
    resolve_parser.add_argument("--role", required=True)
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
        data = load_catalog()
        index = validate_catalog(data)
        if args.command == "validate":
            print(f"Validated {len(index)} project-local skills.")
        elif args.command == "sync":
            synchronize(data, index)
            print(f"Synchronized {len(index)} project-local skills.")
        elif args.command == "list":
            print(json.dumps(list_for_role(data, index, args.role), indent=2))
        elif args.command == "resolve":
            packet = resolve_selection(data, index, args.role, args.skills)
            print(json.dumps(packet, indent=2))
        else:
            parser.error(f"Unknown command: {args.command}")
        return 0
    except SkillConfigurationError as exc:
        print(f"Skill configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
