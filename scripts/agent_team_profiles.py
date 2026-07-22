from __future__ import annotations

"""Deterministic professional-profile resolution and compilation.

Professional context is activation-scoped data behind one stable runtime skill.
Context budgets, runtime model policy, logical capability authority, and seat
allocation remain owned by their existing Agent-Team configuration layers.
"""

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

try:
    from .agent_team_domain import (
        AuditEvent,
        freeze_mapping,
        require_identifier,
        require_oid,
        thaw_json,
    )
    from .agent_team_layout import AgentTeamLayout
    from .agent_team_state import AxStateStore, utc_now
except ImportError:
    from agent_team_domain import (
        AuditEvent,
        freeze_mapping,
        require_identifier,
        require_oid,
        thaw_json,
    )
    from agent_team_layout import AgentTeamLayout
    from agent_team_state import AxStateStore, utc_now


PROFILE_CATALOG_VERSION = 1
COMPILED_PROFILE_VERSION = 1
PROFESSIONAL_SKILL_ID = "professional-profile-runtime"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REFERENCE_ID_PATTERN = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
PROFILE_REFERENCE_ORDER = (
    "role",
    "gate_or_task",
    "primary_technology",
    "secondary_technology",
    "toolchain",
)
STATE_REFERENCE_KINDS = {
    "role": "ROLE",
    "gate_or_task": "GATE_OR_TASK",
    "primary_technology": "PRIMARY_TECHNOLOGY",
    "secondary_technology": "SECONDARY_TECHNOLOGY",
    "toolchain": "TOOLCHAIN",
}


class ProfessionalProfileError(RuntimeError):
    """Base error for professional-profile activation failures."""


class ProfileCatalogError(ProfessionalProfileError):
    """Raised when allowlisted profile data is malformed or changed."""


class ProfileResolutionError(ProfessionalProfileError):
    """Raised when repository evidence cannot select one safe profile."""


class ProfileCompilationError(ProfessionalProfileError):
    """Raised when pinned references cannot compile deterministically."""


class ProfileStateBindingError(ProfessionalProfileError):
    """Raised when an activation binding conflicts with persisted state."""


class ProfileCategory(str, Enum):
    ROLE = "role"
    GATE_OR_TASK = "gate_or_task"
    TECHNOLOGY = "technology"
    TOOLCHAIN = "toolchain"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _selector(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileResolutionError(f"{field} must be a non-empty string")
    return value.strip().casefold()


def _catalog_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileCatalogError(f"{field} must be a non-empty string")
    return value.strip()


def _catalog_strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProfileCatalogError(f"{field} must be a list of non-empty strings")
    normalized = tuple(item.strip().casefold() for item in value)
    if len(normalized) != len(set(normalized)):
        raise ProfileCatalogError(f"{field} must not contain duplicates")
    return normalized


def _request_paths(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ProfileResolutionError(f"{field} must be a sequence of paths")
    result = tuple(_repository_path(value, field) for value in values)
    if len(result) != len(set(result)):
        raise ProfileResolutionError(f"{field} must not contain duplicate paths")
    return result


def _repository_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileResolutionError(f"{field} contains an empty path")
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ProfileResolutionError(
            f"{field} must contain normalized repository-relative paths: {value!r}"
        )
    return path.as_posix()


def _profile_relative_path(value: str, field: str) -> PurePosixPath:
    raw = _catalog_string(value, field).replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ProfileCatalogError(
            f"{field} must be normalized beneath the profile root: {value!r}"
        )
    return path


def _resolve_profile_file(profile_root: Path, relative: str) -> Path:
    logical = _profile_relative_path(relative, "reference path")
    root = profile_root.expanduser().resolve(strict=True)
    candidate = root.joinpath(*logical.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ProfileCatalogError(f"Profile reference is missing: {logical}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProfileCatalogError(
            f"Profile reference escapes the profile root: {logical}"
        ) from exc
    if not resolved.is_file():
        raise ProfileCatalogError(f"Profile reference is not a file: {logical}")
    return resolved


def _category_path_prefix(category: ProfileCategory) -> str:
    return {
        ProfileCategory.ROLE: "roles",
        ProfileCategory.GATE_OR_TASK: "gates",
        ProfileCategory.TECHNOLOGY: "technologies",
        ProfileCategory.TOOLCHAIN: "toolchains",
    }[category]


def _flatten_strings(value: Any) -> tuple[str, ...]:
    result: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, Mapping):
            for child in item.values():
                visit(child)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(result)


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProfileResolutionError(f"{field} must be a non-empty string when supplied")
    return value.strip()


def _selector_values(
    value: Any,
    field: str,
    *,
    allow_scalar: bool,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        if not allow_scalar:
            raise ProfileResolutionError(f"{field} must be a list of strings")
        values = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = tuple(value)
    else:
        raise ProfileResolutionError(
            f"{field} must be {'a string or ' if allow_scalar else ''}a list of strings"
        )
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ProfileResolutionError(f"{field} contains an invalid selector")
    normalized = tuple(item.strip() for item in values)
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ProfileResolutionError(f"{field} contains duplicate selectors")
    return normalized


@dataclass(frozen=True, slots=True)
class ProfileResolutionRequest:
    activation_id: str
    role: str
    gate_or_task: str
    subject_oid: str
    target_paths: tuple[str, ...]
    write_scope: tuple[str, ...]
    repository_manifests: Mapping[str, str]
    build_evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activation_id",
            require_identifier(self.activation_id, "activation_id"),
        )
        object.__setattr__(self, "role", _selector(self.role, "role"))
        object.__setattr__(
            self,
            "gate_or_task",
            _selector(self.gate_or_task, "gate_or_task"),
        )
        object.__setattr__(
            self,
            "subject_oid",
            require_oid(self.subject_oid, "subject_oid"),
        )
        object.__setattr__(
            self,
            "target_paths",
            _request_paths(self.target_paths, "target_paths"),
        )
        object.__setattr__(
            self,
            "write_scope",
            _request_paths(self.write_scope, "write_scope"),
        )
        if not isinstance(self.repository_manifests, Mapping):
            raise ProfileResolutionError("repository_manifests must be an object")
        manifests: dict[str, str] = {}
        for raw_path, content in self.repository_manifests.items():
            path = _repository_path(raw_path, "repository_manifests")
            if not isinstance(content, str):
                raise ProfileResolutionError(
                    f"repository manifest content must be text: {path}"
                )
            if path in manifests:
                raise ProfileResolutionError(
                    f"repository manifest path is duplicated: {path}"
                )
            manifests[path] = content
        object.__setattr__(
            self,
            "repository_manifests",
            MappingProxyType(dict(sorted(manifests.items()))),
        )
        try:
            frozen_evidence = freeze_mapping(self.build_evidence, "build_evidence")
        except Exception as exc:
            raise ProfileResolutionError(str(exc)) from exc
        object.__setattr__(self, "build_evidence", frozen_evidence)


@dataclass(frozen=True, slots=True)
class CatalogProfileReference:
    reference_id: str
    category: ProfileCategory
    version: str
    path: str
    sha256: str
    aliases: tuple[str, ...]
    manifest_names: tuple[str, ...] = ()
    manifest_suffixes: tuple[str, ...] = ()
    manifest_content_markers: tuple[str, ...] = ()
    manifest_requires_content: bool = False
    extensions: tuple[str, ...] = ()
    path_markers: tuple[str, ...] = ()
    build_markers: tuple[str, ...] = ()
    compatible_toolchains: tuple[str, ...] = ()
    default_toolchain: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileReference:
    reference_id: str
    category: ProfileCategory
    version: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not REFERENCE_ID_PATTERN.fullmatch(self.reference_id):
            raise ProfileCatalogError(
                f"invalid pinned profile reference id: {self.reference_id!r}"
            )
        try:
            category = ProfileCategory(self.category)
        except ValueError as exc:
            raise ProfileCatalogError(
                f"invalid pinned profile category: {self.category!r}"
            ) from exc
        object.__setattr__(self, "category", category)
        object.__setattr__(
            self,
            "version",
            _catalog_string(self.version, "pinned reference version"),
        )
        object.__setattr__(
            self,
            "path",
            _profile_relative_path(
                self.path,
                "pinned reference path",
            ).as_posix(),
        )
        digest = _catalog_string(
            self.sha256,
            "pinned reference SHA-256",
        ).lower()
        if not SHA256_PATTERN.fullmatch(digest):
            raise ProfileCatalogError("pinned reference SHA-256 is invalid")
        object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True, slots=True)
class ProfileReferenceSet:
    activation_id: str
    subject_oid: str
    role_ref: ProfileReference
    gate_or_task_ref: ProfileReference
    primary_technology_ref: ProfileReference
    secondary_technology_ref: ProfileReference | None
    toolchain_ref: ProfileReference

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activation_id",
            require_identifier(self.activation_id, "activation_id"),
        )
        object.__setattr__(
            self,
            "subject_oid",
            require_oid(self.subject_oid, "subject_oid"),
        )
        expected_categories = (
            ("role_ref", self.role_ref, ProfileCategory.ROLE),
            (
                "gate_or_task_ref",
                self.gate_or_task_ref,
                ProfileCategory.GATE_OR_TASK,
            ),
            (
                "primary_technology_ref",
                self.primary_technology_ref,
                ProfileCategory.TECHNOLOGY,
            ),
            ("toolchain_ref", self.toolchain_ref, ProfileCategory.TOOLCHAIN),
        )
        for field, reference, category in expected_categories:
            if not isinstance(reference, ProfileReference):
                raise ProfileResolutionError(
                    f"{field} must be a pinned ProfileReference"
                )
            if reference.category is not category:
                raise ProfileResolutionError(
                    f"{field} must have category {category.value}"
                )
        if self.secondary_technology_ref is not None:
            if not isinstance(self.secondary_technology_ref, ProfileReference):
                raise ProfileResolutionError(
                    "secondary_technology_ref must be a pinned ProfileReference"
                )
            if (
                self.secondary_technology_ref.category
                is not ProfileCategory.TECHNOLOGY
            ):
                raise ProfileResolutionError(
                    "secondary_technology_ref must have category technology"
                )
            if (
                self.secondary_technology_ref.reference_id
                == self.primary_technology_ref.reference_id
            ):
                raise ProfileResolutionError(
                    "primary and secondary technology references must differ"
                )
        reference_ids = [
            reference.reference_id
            for _, reference in self.ordered_references
        ]
        if len(reference_ids) != len(set(reference_ids)):
            raise ProfileResolutionError(
                "profile reference set must not contain duplicate references"
            )

    @property
    def ordered_references(self) -> tuple[tuple[str, ProfileReference], ...]:
        result: list[tuple[str, ProfileReference]] = [
            ("role", self.role_ref),
            ("gate_or_task", self.gate_or_task_ref),
            ("primary_technology", self.primary_technology_ref),
        ]
        if self.secondary_technology_ref is not None:
            result.append(
                ("secondary_technology", self.secondary_technology_ref)
            )
        result.append(("toolchain", self.toolchain_ref))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class CompiledProfessionalProfile:
    activation_id: str
    role_ref: str
    gate_or_task_ref: str
    primary_technology_ref: str
    secondary_technology_ref: str | None
    toolchain_ref: str
    reference_digests: tuple[str, ...]
    compiled_path: Path
    compiled_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activation_id",
            require_identifier(self.activation_id, "activation_id"),
        )
        for field in (
            "role_ref",
            "gate_or_task_ref",
            "primary_technology_ref",
            "toolchain_ref",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not REFERENCE_ID_PATTERN.fullmatch(value):
                raise ProfileCompilationError(
                    f"{field} must be a valid profile reference id"
                )
        if self.secondary_technology_ref is not None and not (
            isinstance(self.secondary_technology_ref, str)
            and REFERENCE_ID_PATTERN.fullmatch(self.secondary_technology_ref)
        ):
            raise ProfileCompilationError(
                "secondary_technology_ref must be a valid profile reference id"
            )
        if len(self.reference_digests) not in {4, 5} or any(
            not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            for digest in self.reference_digests
        ):
            raise ProfileCompilationError(
                "reference_digests must contain four or five SHA-256 digests"
            )
        path = Path(self.compiled_path).expanduser().resolve(strict=False)
        object.__setattr__(self, "compiled_path", path)
        if not isinstance(self.compiled_digest, str) or not SHA256_PATTERN.fullmatch(
            self.compiled_digest
        ):
            raise ProfileCompilationError(
                "compiled_digest must be a lowercase SHA-256 digest"
            )


class ProfessionalProfileCatalog:
    """Loads and validates the profile-root reference allowlist."""

    def __init__(self, profile_root: Path, data: Mapping[str, Any]) -> None:
        self.profile_root = profile_root.expanduser().resolve(strict=True)
        catalog = data.get("catalog")
        references = data.get("references")
        if not isinstance(catalog, Mapping):
            raise ProfileCatalogError("profile catalog is missing [catalog]")
        if catalog.get("version") != PROFILE_CATALOG_VERSION:
            raise ProfileCatalogError(
                f"profile catalog version must be {PROFILE_CATALOG_VERSION}"
            )
        if catalog.get("single_skill_id") != PROFESSIONAL_SKILL_ID:
            raise ProfileCatalogError(
                f"profile catalog must bind only {PROFESSIONAL_SKILL_ID}"
            )
        required_non_expansion = (
            "authority_expansion_allowed",
            "tool_expansion_allowed",
            "write_scope_expansion_allowed",
            "model_selection_allowed",
            "gate_expansion_allowed",
        )
        if any(catalog.get(field) is not False for field in required_non_expansion):
            raise ProfileCatalogError(
                "professional profiles must not expand authority, tools, write scope, "
                "models, or gates"
            )
        schema_path = _catalog_string(
            catalog.get("compiled_schema"),
            "catalog.compiled_schema",
        )
        _resolve_profile_file(self.profile_root, schema_path)
        order = _catalog_strings(
            catalog.get("reference_order"),
            "catalog.reference_order",
        )
        if order != PROFILE_REFERENCE_ORDER:
            raise ProfileCatalogError(
                f"catalog.reference_order must be {PROFILE_REFERENCE_ORDER}"
            )
        if catalog.get("max_secondary_technologies") != 1:
            raise ProfileCatalogError(
                "catalog.max_secondary_technologies must be 1"
            )
        if not isinstance(references, list) or not references:
            raise ProfileCatalogError("profile catalog must contain references")
        self._references = self._load_references(references)
        self._selectors = self._build_selector_index(self._references)
        self._validate_required_categories()

    @classmethod
    def load(
        cls,
        profile_root: Path | None = None,
    ) -> "ProfessionalProfileCatalog":
        layout = AgentTeamLayout.discover(Path(__file__))
        root = Path(profile_root or layout.profile_root).expanduser().resolve()
        catalog_path = root / "catalog.toml"
        if not catalog_path.is_file():
            raise ProfileCatalogError(f"Profile catalog is missing: {catalog_path}")
        try:
            with catalog_path.open("rb") as stream:
                data = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileCatalogError(
                f"Profile catalog TOML is invalid: {exc}"
            ) from exc
        return cls(root, data)

    def _load_references(
        self,
        raw_references: Sequence[Any],
    ) -> dict[str, CatalogProfileReference]:
        result: dict[str, CatalogProfileReference] = {}
        for raw in raw_references:
            if not isinstance(raw, Mapping):
                raise ProfileCatalogError("profile reference entries must be objects")
            reference_id = _catalog_string(raw.get("id"), "reference.id")
            if not REFERENCE_ID_PATTERN.fullmatch(reference_id):
                raise ProfileCatalogError(
                    f"invalid profile reference id: {reference_id!r}"
                )
            if reference_id in result:
                raise ProfileCatalogError(
                    f"duplicate profile reference id: {reference_id}"
                )
            try:
                category = ProfileCategory(
                    _catalog_string(raw.get("category"), "reference.category")
                )
            except ValueError as exc:
                raise ProfileCatalogError(
                    f"invalid category for {reference_id}"
                ) from exc
            path = _profile_relative_path(
                _catalog_string(raw.get("path"), "reference.path"),
                "reference.path",
            ).as_posix()
            if path.split("/", 1)[0] != _category_path_prefix(category):
                raise ProfileCatalogError(
                    f"{reference_id} path does not match category {category.value}"
                )
            digest = _catalog_string(raw.get("sha256"), "reference.sha256").lower()
            if not SHA256_PATTERN.fullmatch(digest):
                raise ProfileCatalogError(
                    f"{reference_id} has an invalid SHA-256 digest"
                )
            resolved = _resolve_profile_file(self.profile_root, path)
            observed = _sha256_bytes(resolved.read_bytes())
            if observed != digest:
                raise ProfileCatalogError(
                    f"{reference_id} digest mismatch: expected {digest}, observed {observed}"
                )
            aliases = _catalog_strings(raw.get("aliases"), "reference.aliases")
            manifest_requires_content = raw.get("manifest_requires_content", False)
            if not isinstance(manifest_requires_content, bool):
                raise ProfileCatalogError(
                    f"{reference_id} manifest_requires_content must be boolean"
                )
            default_toolchain = raw.get("default_toolchain")
            if default_toolchain is not None:
                default_toolchain = _catalog_string(
                    default_toolchain,
                    f"{reference_id}.default_toolchain",
                ).casefold()
            reference = CatalogProfileReference(
                reference_id=reference_id,
                category=category,
                version=_catalog_string(raw.get("version"), "reference.version"),
                path=path,
                sha256=digest,
                aliases=aliases,
                manifest_names=_catalog_strings(
                    raw.get("manifest_names", []),
                    f"{reference_id}.manifest_names",
                ),
                manifest_suffixes=_catalog_strings(
                    raw.get("manifest_suffixes", []),
                    f"{reference_id}.manifest_suffixes",
                ),
                manifest_content_markers=_catalog_strings(
                    raw.get("manifest_content_markers", []),
                    f"{reference_id}.manifest_content_markers",
                ),
                manifest_requires_content=manifest_requires_content,
                extensions=_catalog_strings(
                    raw.get("extensions", []),
                    f"{reference_id}.extensions",
                ),
                path_markers=_catalog_strings(
                    raw.get("path_markers", []),
                    f"{reference_id}.path_markers",
                ),
                build_markers=_catalog_strings(
                    raw.get("build_markers", []),
                    f"{reference_id}.build_markers",
                ),
                compatible_toolchains=_catalog_strings(
                    raw.get("compatible_toolchains", []),
                    f"{reference_id}.compatible_toolchains",
                ),
                default_toolchain=default_toolchain,
            )
            self._validate_reference_shape(reference)
            result[reference_id] = reference
        return result

    @staticmethod
    def _validate_reference_shape(reference: CatalogProfileReference) -> None:
        short_id = reference.reference_id.split("/", 1)[1]
        if reference.category is ProfileCategory.TECHNOLOGY:
            if (
                not reference.compatible_toolchains
                or reference.default_toolchain is None
                or reference.default_toolchain not in reference.compatible_toolchains
            ):
                raise ProfileCatalogError(
                    f"{reference.reference_id} must declare compatible/default toolchains"
                )
        elif any(
            (
                reference.manifest_names,
                reference.manifest_suffixes,
                reference.manifest_content_markers,
                reference.extensions,
                reference.path_markers,
                reference.build_markers,
                reference.compatible_toolchains,
            )
        ) or reference.default_toolchain is not None:
            if reference.category is not ProfileCategory.TOOLCHAIN:
                raise ProfileCatalogError(
                    f"non-technology reference has technology signals: {reference.reference_id}"
                )
        if short_id not in reference.aliases:
            raise ProfileCatalogError(
                f"{reference.reference_id} aliases must include {short_id!r}"
            )

    @staticmethod
    def _build_selector_index(
        references: Mapping[str, CatalogProfileReference],
    ) -> dict[ProfileCategory, dict[str, str]]:
        result = {category: {} for category in ProfileCategory}
        for reference in references.values():
            selectors = (reference.reference_id, *reference.aliases)
            for selector in selectors:
                key = selector.casefold()
                existing = result[reference.category].get(key)
                if existing is not None and existing != reference.reference_id:
                    raise ProfileCatalogError(
                        f"ambiguous {reference.category.value} selector {selector!r}"
                    )
                result[reference.category][key] = reference.reference_id
        return result

    def _validate_required_categories(self) -> None:
        categories = {reference.category for reference in self._references.values()}
        if categories != set(ProfileCategory):
            raise ProfileCatalogError(
                "profile catalog must contain role, gate/task, technology, and toolchain references"
            )
        toolchains = {
            reference.reference_id.split("/", 1)[1]
            for reference in self._references.values()
            if reference.category is ProfileCategory.TOOLCHAIN
        }
        for reference in self.technology_references:
            missing = set(reference.compatible_toolchains) - toolchains
            if missing:
                raise ProfileCatalogError(
                    f"{reference.reference_id} references missing toolchains: {sorted(missing)}"
                )

    @property
    def technology_references(self) -> tuple[CatalogProfileReference, ...]:
        return tuple(
            sorted(
                (
                    reference
                    for reference in self._references.values()
                    if reference.category is ProfileCategory.TECHNOLOGY
                ),
                key=lambda reference: reference.reference_id,
            )
        )

    def select(
        self,
        category: ProfileCategory,
        selector: str,
    ) -> CatalogProfileReference:
        key = _selector(selector, f"{category.value} selector")
        try:
            reference_id = self._selectors[category][key]
        except KeyError as exc:
            raise ProfileResolutionError(
                f"unknown {category.value} profile selector: {selector!r}"
            ) from exc
        return self._references[reference_id]

    def by_id(self, reference_id: str) -> CatalogProfileReference:
        try:
            return self._references[reference_id]
        except KeyError as exc:
            raise ProfileCatalogError(
                f"profile reference is no longer allowlisted: {reference_id}"
            ) from exc

    def pin(self, reference: CatalogProfileReference) -> ProfileReference:
        return ProfileReference(
            reference_id=reference.reference_id,
            category=reference.category,
            version=reference.version,
            path=reference.path,
            sha256=reference.sha256,
        )

    def read_pinned(self, pinned: ProfileReference) -> str:
        current = self.by_id(pinned.reference_id)
        signature = (
            current.category,
            current.version,
            current.path,
            current.sha256,
        )
        expected = (
            pinned.category,
            pinned.version,
            pinned.path,
            pinned.sha256,
        )
        if signature != expected:
            raise ProfileCompilationError(
                f"allowlisted profile reference changed after resolution: {pinned.reference_id}"
            )
        path = _resolve_profile_file(self.profile_root, pinned.path)
        raw = path.read_bytes()
        observed = _sha256_bytes(raw)
        if observed != pinned.sha256:
            raise ProfileCompilationError(
                f"profile reference digest changed after resolution: {pinned.reference_id}"
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProfileCompilationError(
                f"profile reference is not UTF-8: {pinned.reference_id}"
            ) from exc
        return content.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(slots=True)
class _TechnologyEvidence:
    scores: dict[str, int]
    sources: dict[str, set[str]]

    @classmethod
    def create(
        cls,
        technologies: Sequence[CatalogProfileReference],
    ) -> "_TechnologyEvidence":
        ids = [reference.reference_id for reference in technologies]
        return cls(
            scores={reference_id: 0 for reference_id in ids},
            sources={reference_id: set() for reference_id in ids},
        )

    def add(self, reference_id: str, *, score: int, source: str) -> None:
        self.scores[reference_id] += score
        self.sources[reference_id].add(source)

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                reference_id
                for reference_id, score in self.scores.items()
                if score > 0
            )
        )


class ProfessionalProfileResolver:
    """Resolve one fail-closed professional reference set from repository evidence."""

    def __init__(
        self,
        catalog: ProfessionalProfileCatalog | None = None,
    ) -> None:
        self.catalog = catalog or ProfessionalProfileCatalog.load()

    def resolve(self, request: ProfileResolutionRequest) -> ProfileReferenceSet:
        if not isinstance(request, ProfileResolutionRequest):
            raise TypeError("request must be a ProfileResolutionRequest")
        self._validate_evidence_oid(request)
        role = self.catalog.select(ProfileCategory.ROLE, request.role)
        gate = self.catalog.select(
            ProfileCategory.GATE_OR_TASK,
            request.gate_or_task,
        )
        evidence = self._collect_evidence(request)
        primary, secondary = self._select_technologies(request, evidence)
        toolchain = self._select_toolchain(request, primary, secondary)
        return ProfileReferenceSet(
            activation_id=request.activation_id,
            subject_oid=request.subject_oid,
            role_ref=self.catalog.pin(role),
            gate_or_task_ref=self.catalog.pin(gate),
            primary_technology_ref=self.catalog.pin(primary),
            secondary_technology_ref=(
                self.catalog.pin(secondary) if secondary is not None else None
            ),
            toolchain_ref=self.catalog.pin(toolchain),
        )

    @staticmethod
    def _validate_evidence_oid(request: ProfileResolutionRequest) -> None:
        for field in ("subject_oid", "repository_manifests_oid", "build_oid"):
            value = request.build_evidence.get(field)
            if value is None:
                continue
            try:
                evidence_oid = require_oid(value, f"build_evidence.{field}")
            except Exception as exc:
                raise ProfileResolutionError(str(exc)) from exc
            if evidence_oid != request.subject_oid:
                raise ProfileResolutionError(
                    f"build_evidence.{field} does not match request.subject_oid"
                )

    def _collect_evidence(
        self,
        request: ProfileResolutionRequest,
    ) -> _TechnologyEvidence:
        technologies = self.catalog.technology_references
        evidence = _TechnologyEvidence.create(technologies)
        for path, content in request.repository_manifests.items():
            self._collect_manifest_evidence(evidence, technologies, path, content)
        for path in request.target_paths:
            self._collect_path_evidence(
                evidence,
                technologies,
                path,
                score=12,
                source_prefix="target",
            )
        for path in request.write_scope:
            self._collect_path_evidence(
                evidence,
                technologies,
                path,
                score=18,
                source_prefix="write",
            )
        self._collect_build_evidence(evidence, technologies, request.build_evidence)
        return evidence

    @staticmethod
    def _collect_manifest_evidence(
        evidence: _TechnologyEvidence,
        technologies: Sequence[CatalogProfileReference],
        path: str,
        content: str,
    ) -> None:
        normalized_path = path.casefold()
        basename = PurePosixPath(normalized_path).name
        normalized_content = content.casefold()
        for reference in technologies:
            name_match = basename in reference.manifest_names
            suffix_match = any(
                normalized_path.endswith(suffix)
                for suffix in reference.manifest_suffixes
            )
            content_match = any(
                marker in normalized_content
                for marker in reference.manifest_content_markers
            )
            structural_match = name_match or suffix_match
            if structural_match and (
                not reference.manifest_requires_content or content_match
            ):
                evidence.add(
                    reference.reference_id,
                    score=40,
                    source=f"manifest:{path}",
                )
            if content_match and (
                structural_match
                or reference.reference_id == "technology/local-data"
            ):
                evidence.add(
                    reference.reference_id,
                    score=15,
                    source=f"manifest-content:{path}",
                )

    @staticmethod
    def _collect_path_evidence(
        evidence: _TechnologyEvidence,
        technologies: Sequence[CatalogProfileReference],
        path: str,
        *,
        score: int,
        source_prefix: str,
    ) -> None:
        normalized = f"/{path.casefold()}"
        suffix = PurePosixPath(path.casefold()).suffix
        for reference in technologies:
            if suffix and suffix in reference.extensions:
                evidence.add(
                    reference.reference_id,
                    score=score,
                    source=f"{source_prefix}-extension:{path}",
                )
            if any(marker in normalized for marker in reference.path_markers):
                evidence.add(
                    reference.reference_id,
                    score=score,
                    source=f"{source_prefix}-marker:{path}",
                )

    def _collect_build_evidence(
        self,
        evidence: _TechnologyEvidence,
        technologies: Sequence[CatalogProfileReference],
        build_evidence: Mapping[str, Any],
    ) -> None:
        declared = [
            *_selector_values(
                build_evidence.get("technology"),
                "build_evidence.technology",
                allow_scalar=True,
            ),
            *_selector_values(
                build_evidence.get("technologies"),
                "build_evidence.technologies",
                allow_scalar=False,
            ),
        ]
        for selector in declared:
            reference = self.catalog.select(ProfileCategory.TECHNOLOGY, selector)
            evidence.add(
                reference.reference_id,
                score=60,
                source=f"build-declared:{selector}",
            )
        primary_selector = _optional_string(
            build_evidence.get("primary_technology"),
            "build_evidence.primary_technology",
        )
        if primary_selector is not None:
            primary = self.catalog.select(
                ProfileCategory.TECHNOLOGY,
                primary_selector,
            )
            evidence.add(
                primary.reference_id,
                score=100,
                source="build-primary",
            )
        secondary_selector = _optional_string(
            build_evidence.get("secondary_technology"),
            "build_evidence.secondary_technology",
        )
        if secondary_selector is not None:
            secondary = self.catalog.select(
                ProfileCategory.TECHNOLOGY,
                secondary_selector,
            )
            evidence.add(
                secondary.reference_id,
                score=90,
                source="build-secondary",
            )
        searchable_fields = (
            "command",
            "commands",
            "executables",
            "tool_versions",
            "artifacts",
        )
        searchable = "\n".join(
            text.casefold()
            for field in searchable_fields
            for text in _flatten_strings(build_evidence.get(field))
        )
        for reference in technologies:
            for marker in reference.build_markers:
                if self._contains_build_marker(searchable, marker):
                    evidence.add(
                        reference.reference_id,
                        score=30,
                        source=f"build-marker:{marker}",
                    )

    @staticmethod
    def _contains_build_marker(searchable: str, marker: str) -> bool:
        if not searchable:
            return False
        if re.fullmatch(r"[a-z0-9_.+-]+", marker):
            pattern = rf"(?<![a-z0-9_.+-]){re.escape(marker)}(?![a-z0-9_.+-])"
            return re.search(pattern, searchable) is not None
        return marker in searchable

    def _select_technologies(
        self,
        request: ProfileResolutionRequest,
        evidence: _TechnologyEvidence,
    ) -> tuple[CatalogProfileReference, CatalogProfileReference | None]:
        candidates = list(evidence.candidates)
        if not candidates:
            raise ProfileResolutionError(
                "technology evidence is missing; activation is blocked"
            )
        if len(candidates) > 2:
            details = {
                reference_id: sorted(evidence.sources[reference_id])
                for reference_id in candidates
            }
            raise ProfileResolutionError(
                "technology evidence selects more than one primary and one secondary: "
                f"{details}"
            )
        explicit_primary = _optional_string(
            request.build_evidence.get("primary_technology"),
            "build_evidence.primary_technology",
        )
        explicit_secondary = _optional_string(
            request.build_evidence.get("secondary_technology"),
            "build_evidence.secondary_technology",
        )
        if explicit_secondary is not None and explicit_primary is None:
            raise ProfileResolutionError(
                "secondary_technology requires an explicit primary_technology"
            )
        if explicit_primary is not None:
            primary = self.catalog.select(
                ProfileCategory.TECHNOLOGY,
                explicit_primary,
            )
            if primary.reference_id not in candidates:
                raise ProfileResolutionError(
                    "explicit primary technology has no repository or build evidence"
                )
            if explicit_secondary is not None:
                secondary = self.catalog.select(
                    ProfileCategory.TECHNOLOGY,
                    explicit_secondary,
                )
                if secondary.reference_id == primary.reference_id:
                    raise ProfileResolutionError(
                        "primary and secondary technology must be different"
                    )
                if secondary.reference_id not in candidates:
                    raise ProfileResolutionError(
                        "explicit secondary technology has no repository or build evidence"
                    )
                if set(candidates) != {
                    primary.reference_id,
                    secondary.reference_id,
                }:
                    raise ProfileResolutionError(
                        "explicit technology selection conflicts with detected evidence"
                    )
                return primary, secondary
            remaining = [
                reference_id
                for reference_id in candidates
                if reference_id != primary.reference_id
            ]
            secondary = self.catalog.by_id(remaining[0]) if remaining else None
            return primary, secondary
        if len(candidates) == 1:
            return self.catalog.by_id(candidates[0]), None
        ranked = sorted(
            candidates,
            key=lambda reference_id: (
                -evidence.scores[reference_id],
                reference_id,
            ),
        )
        if evidence.scores[ranked[0]] == evidence.scores[ranked[1]]:
            details = {
                reference_id: {
                    "score": evidence.scores[reference_id],
                    "sources": sorted(evidence.sources[reference_id]),
                }
                for reference_id in ranked
            }
            raise ProfileResolutionError(
                f"primary technology evidence is ambiguous: {details}"
            )
        return self.catalog.by_id(ranked[0]), self.catalog.by_id(ranked[1])

    def _select_toolchain(
        self,
        request: ProfileResolutionRequest,
        primary: CatalogProfileReference,
        secondary: CatalogProfileReference | None,
    ) -> CatalogProfileReference:
        selectors: list[str] = []
        singular = _optional_string(
            request.build_evidence.get("toolchain"),
            "build_evidence.toolchain",
        )
        if singular is not None:
            selectors.append(singular)
        if "toolchains" in request.build_evidence:
            selectors.extend(
                _selector_values(
                    request.build_evidence.get("toolchains"),
                    "build_evidence.toolchains",
                    allow_scalar=False,
                )
            )
        if len({selector.casefold() for selector in selectors}) != len(selectors):
            raise ProfileResolutionError(
                "toolchain evidence contains duplicate selectors"
            )
        normalized = {
            self.catalog.select(ProfileCategory.TOOLCHAIN, selector).reference_id
            for selector in selectors
        }
        if len(normalized) > 1:
            raise ProfileResolutionError(
                f"conflicting toolchain evidence: {sorted(normalized)}"
            )
        if normalized:
            toolchain = self.catalog.by_id(next(iter(normalized)))
        else:
            secondary_default = (
                secondary.default_toolchain if secondary is not None else None
            )
            if (
                secondary_default is not None
                and secondary_default in primary.compatible_toolchains
            ):
                selected_default = secondary_default
            elif len(primary.compatible_toolchains) == 1:
                selected_default = primary.compatible_toolchains[0]
            else:
                raise ProfileResolutionError(
                    f"{primary.reference_id} requires explicit or secondary-technology "
                    "toolchain evidence"
                )
            toolchain = self.catalog.select(
                ProfileCategory.TOOLCHAIN,
                selected_default,
            )
        toolchain_id = toolchain.reference_id.split("/", 1)[1]
        if toolchain_id not in primary.compatible_toolchains:
            raise ProfileResolutionError(
                f"toolchain {toolchain.reference_id} conflicts with {primary.reference_id}"
            )
        return toolchain


class ProfessionalProfileCompiler:
    """Compile pinned profile fragments into one immutable activation artifact."""

    def __init__(
        self,
        catalog: ProfessionalProfileCatalog | None = None,
        *,
        state_store: AxStateStore | None = None,
    ) -> None:
        self.catalog = catalog or ProfessionalProfileCatalog.load()
        self.state_store = state_store

    def compile(
        self,
        resolution: ProfileReferenceSet,
        *,
        activation_root: Path,
    ) -> CompiledProfessionalProfile:
        if not isinstance(resolution, ProfileReferenceSet):
            raise TypeError("resolution must be a ProfileReferenceSet")
        root = Path(activation_root).expanduser()
        if root.exists() and not root.is_dir():
            raise ProfileCompilationError(
                f"activation_root is not a directory: {root}"
            )
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        entries: list[dict[str, Any]] = []
        for binding_kind, reference in resolution.ordered_references:
            content = self.catalog.read_pinned(reference)
            entries.append(
                {
                    "category": binding_kind,
                    "content": content,
                    "id": reference.reference_id,
                    "path": reference.path,
                    "sha256": reference.sha256,
                    "version": reference.version,
                }
            )
        document = {
            "gate_or_task_ref": resolution.gate_or_task_ref.reference_id,
            "primary_technology_ref": (
                resolution.primary_technology_ref.reference_id
            ),
            "professional_skill_id": PROFESSIONAL_SKILL_ID,
            "references": entries,
            "role_ref": resolution.role_ref.reference_id,
            "schema_version": COMPILED_PROFILE_VERSION,
            "secondary_technology_ref": (
                resolution.secondary_technology_ref.reference_id
                if resolution.secondary_technology_ref is not None
                else None
            ),
            "toolchain_ref": resolution.toolchain_ref.reference_id,
        }
        compiled_bytes = (
            json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        digest = _sha256_bytes(compiled_bytes)
        target_candidate = root / "professional-profile.json"
        if target_candidate.is_symlink():
            raise ProfileCompilationError(
                f"compiled profile target must not be a symlink: {target_candidate}"
            )
        target = target_candidate.resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ProfileCompilationError(
                f"compiled profile target escapes activation_root: {target}"
            ) from exc
        temporary = root / ".professional-profile.json.tmp"
        if temporary.exists() and temporary.is_symlink():
            raise ProfileCompilationError(
                f"compiled profile temporary target must not be a symlink: {temporary}"
            )
        temporary.write_bytes(compiled_bytes)
        os.replace(temporary, target)
        compiled = CompiledProfessionalProfile(
            activation_id=resolution.activation_id,
            role_ref=resolution.role_ref.reference_id,
            gate_or_task_ref=resolution.gate_or_task_ref.reference_id,
            primary_technology_ref=(
                resolution.primary_technology_ref.reference_id
            ),
            secondary_technology_ref=(
                resolution.secondary_technology_ref.reference_id
                if resolution.secondary_technology_ref is not None
                else None
            ),
            toolchain_ref=resolution.toolchain_ref.reference_id,
            reference_digests=tuple(
                reference.sha256
                for _, reference in resolution.ordered_references
            ),
            compiled_path=target,
            compiled_digest=digest,
        )
        if self.state_store is not None:
            self._persist_binding(resolution, compiled)
        return compiled

    def _persist_binding(
        self,
        resolution: ProfileReferenceSet,
        compiled: CompiledProfessionalProfile,
    ) -> None:
        if self.state_store is None:
            return
        references = [
            {
                "kind": binding_kind,
                "path": reference.path,
                "sha256": reference.sha256,
                "version": reference.version,
            }
            for binding_kind, reference in resolution.ordered_references
        ]
        idempotency_key = f"profile-binding:{resolution.activation_id}"
        intent = self.state_store.begin_intent(
            operation="bind-professional-profile",
            idempotency_key=idempotency_key,
            expected_state="CREATED",
            expected_oid=resolution.subject_oid,
            payload={
                "activation_id": resolution.activation_id,
                "compiled_profile_digest": compiled.compiled_digest,
                "compiled_profile_ref": str(compiled.compiled_path),
                "references": references,
            },
        )
        with self.state_store.transaction(immediate=True) as connection:
            activation = connection.execute(
                """
                SELECT id, subject_oid, state
                FROM activations
                WHERE id = ?
                """,
                (resolution.activation_id,),
            ).fetchone()
            if activation is None:
                raise ProfileStateBindingError(
                    f"activation does not exist: {resolution.activation_id}"
                )
            if activation["subject_oid"] != resolution.subject_oid:
                raise ProfileStateBindingError(
                    "activation subject OID conflicts with profile resolution"
                )
            if activation["state"] not in {"CREATED", "PROFILE_BOUND"}:
                raise ProfileStateBindingError(
                    f"activation cannot bind a profile from {activation['state']}"
                )
            existing = connection.execute(
                """
                SELECT professional_skill_id, compiled_profile_ref,
                       compiled_profile_digest, state
                FROM profile_bindings
                WHERE activation_id = ?
                """,
                (resolution.activation_id,),
            ).fetchone()
            expected_binding = (
                PROFESSIONAL_SKILL_ID,
                str(compiled.compiled_path),
                compiled.compiled_digest,
                "BOUND",
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO profile_bindings (
                        activation_id, professional_skill_id,
                        compiled_profile_ref, compiled_profile_digest,
                        state, bound_at
                    ) VALUES (?, ?, ?, ?, 'BOUND', ?)
                    """,
                    (
                        resolution.activation_id,
                        PROFESSIONAL_SKILL_ID,
                        str(compiled.compiled_path),
                        compiled.compiled_digest,
                        utc_now(),
                    ),
                )
            elif tuple(existing) != expected_binding:
                raise ProfileStateBindingError(
                    "activation already has a different professional profile binding"
                )
            existing_references = connection.execute(
                """
                SELECT ordinal, reference_kind, reference_path,
                       reference_version, reference_sha256
                FROM profile_reference_bindings
                WHERE activation_id = ?
                ORDER BY ordinal
                """,
                (resolution.activation_id,),
            ).fetchall()
            expected_references = [
                (
                    ordinal,
                    STATE_REFERENCE_KINDS[binding_kind],
                    reference.path,
                    reference.version,
                    reference.sha256,
                )
                for ordinal, (binding_kind, reference) in enumerate(
                    resolution.ordered_references
                )
            ]
            if existing_references:
                if [tuple(row) for row in existing_references] != expected_references:
                    raise ProfileStateBindingError(
                        "activation already has different pinned profile references"
                    )
            else:
                connection.executemany(
                    """
                    INSERT INTO profile_reference_bindings (
                        activation_id, ordinal, reference_kind,
                        reference_path, reference_version, reference_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (resolution.activation_id, *reference)
                        for reference in expected_references
                    ],
                )
            if activation["state"] == "CREATED":
                updated = connection.execute(
                    """
                    UPDATE activations
                    SET state = 'PROFILE_BOUND', updated_at = ?
                    WHERE id = ? AND state = 'CREATED'
                    """,
                    (utc_now(), resolution.activation_id),
                ).rowcount
                if updated != 1:
                    raise ProfileStateBindingError(
                        "activation profile transition lost a concurrent race"
                    )
        self.state_store.complete_intent(
            intent.intent_id,
            resulting_state="PROFILE_BOUND",
            resulting_oid=resolution.subject_oid,
            evidence={
                "compiled_profile_digest": compiled.compiled_digest,
                "compiled_profile_ref": str(compiled.compiled_path),
            },
        )
        self.state_store.record_audit_event(
            AuditEvent(
                event_id=_stable_identifier(
                    "profile-binding",
                    resolution.activation_id,
                ),
                event_type="PROFESSIONAL_PROFILE_BOUND",
                actor="service:profile-compiler",
                subject_type="activation",
                subject_id=resolution.activation_id,
                payload={
                    "compiled_profile_digest": compiled.compiled_digest,
                    "compiled_profile_ref": str(compiled.compiled_path),
                    "professional_skill_id": PROFESSIONAL_SKILL_ID,
                    "references": thaw_json(references),
                },
                occurred_at=intent.created_at,
                idempotency_key=f"profile-binding-audit:{resolution.activation_id}",
                activation_id=resolution.activation_id,
                subject_oid=resolution.subject_oid,
            )
        )


def _stable_identifier(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def professional_profile_binding(
    compiled: CompiledProfessionalProfile,
) -> dict[str, str]:
    if not isinstance(compiled, CompiledProfessionalProfile):
        raise TypeError("compiled must be a CompiledProfessionalProfile")
    return {
        "skill_id": PROFESSIONAL_SKILL_ID,
        "compiled_profile_ref": str(compiled.compiled_path),
        "compiled_profile_digest": compiled.compiled_digest,
    }
