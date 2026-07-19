from __future__ import annotations

"""Local, evidence-preserving research ledger for the agent-team bundle.

The ledger deliberately separates compact durable records from material content:

* SQLite stores identifiers, provenance, hashes, ratios, and relationships.
* The artifact root stores raw source bytes and every text-bearing artifact.
* CLI JSON contains artifact references only; it never includes raw source or
  summary text that could be copied into a queue message by accident.

This module has no dependency on the agent message queue.  A future Context
Compiler can consume the ``select-context`` output and materialize only the
referenced local artifacts that fit its own role-specific budget.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_STATUSES = frozenset({"ACTIVE", "FINALIZED"})
CONFLICT_STATUSES = frozenset({"OPEN", "RESOLVED", "QUALIFIED", "UNRESOLVED"})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
ARTIFACT_KINDS = frozenset(
    {
        "raw-source",
        "normalized-text",
        "source-shard",
        "shard-summary",
        "claim-statement",
        "conflict-description",
        "conflict-resolution",
        "final-conclusion",
        "research-brief",
    }
)


class ResearchError(ValueError):
    """Raised when a research-ledger contract is invalid or incomplete."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def require_nonempty(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchError(f"{field} must be a non-empty string")
    return value.strip()


def require_identifier(value: str | None, field: str) -> str:
    result = require_nonempty(value, field)
    if not ID_PATTERN.fullmatch(result):
        raise ResearchError(
            f"{field} must match {ID_PATTERN.pattern!r} and be at most 128 characters"
        )
    return result


def generated_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_text_argument(
    *,
    inline: str | None,
    file_path: str | None,
    label: str,
) -> str:
    if (inline is None) == (file_path is None):
        raise ResearchError(f"provide exactly one of --{label} or --{label}-file")
    if inline is not None:
        if not inline:
            raise ResearchError(f"--{label} must not be empty")
        return inline
    path = Path(str(file_path)).expanduser().resolve()
    if not path.is_file():
        raise ResearchError(f"{label} file does not exist: {path}")
    try:
        value = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ResearchError(f"{label} file must be UTF-8 text: {path}") from exc
    if not value:
        raise ResearchError(f"{label} file must not be empty: {path}")
    return value


def redact_url(value: str) -> str:
    """Retain source identity while avoiding credential-like query values in SQLite."""

    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ResearchError("URLs with embedded credentials are not accepted")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResearchError("URL must be an absolute http or https URL")
    redacted_pairs = [(key, "[REDACTED]") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    query = urlencode(redacted_pairs, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def decode_source_text(raw: bytes, *, encoding: str) -> str:
    if b"\x00" in raw and encoding == "auto":
        raise ResearchError(
            "source looks binary; provide an extracted UTF-8 text file or explicitly set --encoding"
        )
    encodings = [encoding] if encoding != "auto" else ["utf-8-sig", "utf-16"]
    errors: list[str] = []
    for candidate in encodings:
        try:
            text = raw.decode(candidate, errors="strict")
        except UnicodeDecodeError as exc:
            errors.append(f"{candidate}: {exc.reason}")
            continue
        if not text:
            raise ResearchError("source text is empty")
        return text
    raise ResearchError(
        "source cannot be decoded as text (" + "; ".join(errors) + "). "
        "Supply a text extraction or use --encoding explicitly."
    )


def split_text(text: str, *, max_chars: int, overlap_chars: int) -> list[tuple[int, int, str]]:
    """Split text at a nearby whitespace boundary without silently losing data."""

    if max_chars <= 0:
        raise ResearchError("--max-chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ResearchError("--overlap-chars must be non-negative and smaller than --max-chars")
    if not text:
        raise ResearchError("cannot shard empty text")

    result: list[tuple[int, int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        nominal_end = min(start + max_chars, length)
        end = nominal_end
        if nominal_end < length:
            lower_bound = start + max(1, max_chars // 2)
            boundary = max(text.rfind("\n", lower_bound, nominal_end), text.rfind(" ", lower_bound, nominal_end))
            if boundary > start:
                end = boundary + 1
        if end <= start:
            end = nominal_end
        shard = text[start:end]
        if not shard:
            raise ResearchError("shard splitting produced an empty shard")
        result.append((start, end, shard))
        if end == length:
            break
        next_start = end - overlap_chars
        if next_start <= start:
            next_start = end
        start = next_start
    return result


class ResearchLedger:
    """SQLite metadata ledger plus a local, hash-addressed artifact directory."""

    def __init__(self, db_path: str | Path, artifact_root: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.artifact_root = Path(artifact_root).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> dict[str, Any]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_runs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    question_artifact_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'FINALIZED')),
                    created_at TEXT NOT NULL,
                    finalized_at TEXT,
                    conclusion_artifact_id TEXT,
                    finalization_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL,
                    byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
                    char_count INTEGER,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK(source_type IN ('file', 'url')),
                    origin TEXT NOT NULL,
                    raw_artifact_id TEXT NOT NULL UNIQUE,
                    normalized_text_artifact_id TEXT UNIQUE,
                    content_type TEXT NOT NULL,
                    text_chars INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE RESTRICT,
                    FOREIGN KEY(raw_artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT,
                    FOREIGN KEY(normalized_text_artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS shards (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    char_start INTEGER NOT NULL CHECK(char_start >= 0),
                    char_end INTEGER NOT NULL CHECK(char_end > char_start),
                    artifact_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, ordinal),
                    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE RESTRICT,
                    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id TEXT PRIMARY KEY,
                    shard_id TEXT NOT NULL UNIQUE,
                    artifact_id TEXT NOT NULL UNIQUE,
                    target_chars INTEGER NOT NULL CHECK(target_chars >= 0),
                    actual_chars INTEGER NOT NULL CHECK(actual_chars >= 0),
                    actual_ratio REAL NOT NULL CHECK(actual_ratio >= 0),
                    advisory_absolute_limit INTEGER NOT NULL CHECK(advisory_absolute_limit > 0),
                    over_target INTEGER NOT NULL CHECK(over_target IN (0, 1)),
                    over_advisory_absolute_limit INTEGER NOT NULL
                        CHECK(over_advisory_absolute_limit IN (0, 1)),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(shard_id) REFERENCES shards(id) ON DELETE RESTRICT,
                    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    statement_artifact_id TEXT NOT NULL UNIQUE,
                    confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'SUPERSEDED')),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE RESTRICT,
                    FOREIGN KEY(statement_artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('summary', 'shard')),
                    evidence_id TEXT NOT NULL,
                    PRIMARY KEY(claim_id, evidence_kind, evidence_id),
                    FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS conflicts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    description_artifact_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('OPEN', 'RESOLVED', 'QUALIFIED', 'UNRESOLVED')),
                    resolution_artifact_id TEXT UNIQUE,
                    opened_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE RESTRICT,
                    FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE RESTRICT,
                    FOREIGN KEY(description_artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT,
                    FOREIGN KEY(resolution_artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS conflict_evidence (
                    conflict_id TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('supporting', 'contradicting', 'resolution')),
                    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('summary', 'shard')),
                    evidence_id TEXT NOT NULL,
                    PRIMARY KEY(conflict_id, side, evidence_kind, evidence_id),
                    FOREIGN KEY(conflict_id) REFERENCES conflicts(id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS ix_sources_run ON sources(run_id, created_at);
                CREATE INDEX IF NOT EXISTS ix_shards_source ON shards(source_id, ordinal);
                CREATE INDEX IF NOT EXISTS ix_claims_run ON claims(run_id, status, created_at);
                CREATE INDEX IF NOT EXISTS ix_conflicts_run ON conflicts(run_id, status, opened_at);
                """
            )
            version_row = connection.execute(
                "SELECT value FROM ledger_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version_row is None:
                connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif version_row["value"] != str(SCHEMA_VERSION):
                raise ResearchError(
                    f"unsupported research ledger schema version {version_row['value']}"
                )
        return {
            "status": "initialized",
            "db": str(self.db_path),
            "artifact_root": str(self.artifact_root),
            "schema_version": SCHEMA_VERSION,
        }

    def _require_initialized(self) -> None:
        if not self.db_path.is_file():
            raise ResearchError("ledger is not initialized; run init first")
        if not self.artifact_root.is_dir():
            raise ResearchError("artifact root is missing; run init first")

    def _run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ResearchError(f"unknown research run: {run_id}")
        return row

    def _require_active_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = self._run_row(connection, run_id)
        if row["status"] != "ACTIVE":
            raise ResearchError(f"research run is not active: {run_id}")
        return row

    def _artifact_ref(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["id"],
            "uri": f"artifact://research/{row['id']}",
            "kind": row["kind"],
            "relative_path": row["relative_path"],
            "sha256": row["sha256"],
            "byte_count": row["byte_count"],
            "char_count": row["char_count"],
            "content_type": row["content_type"],
        }

    def _artifact_row(self, connection: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise ResearchError(f"unknown artifact: {artifact_id}")
        return row

    def _safe_destination(self, kind: str, artifact_id: str, suffix: str) -> tuple[Path, str]:
        if kind not in ARTIFACT_KINDS:
            raise ResearchError(f"unsupported artifact kind: {kind}")
        if not suffix.startswith(".") or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
            raise ResearchError(f"unsafe artifact suffix: {suffix!r}")
        destination = (self.artifact_root / kind / f"{artifact_id}{suffix}").resolve()
        try:
            relative = destination.relative_to(self.artifact_root).as_posix()
        except ValueError as exc:
            raise ResearchError("artifact destination escapes artifact root") from exc
        return destination, relative

    def _write_artifact_bytes(
        self,
        *,
        artifact_id: str,
        kind: str,
        raw: bytes,
        suffix: str,
    ) -> tuple[Path, str, str]:
        destination, relative = self._safe_destination(kind, artifact_id, suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ResearchError(f"artifact destination already exists: {relative}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{artifact_id}-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return destination, relative, sha256_bytes(raw)

    def _store_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        raw: bytes,
        content_type: str,
        char_count: int | None,
        suffix: str,
        artifact_id: str | None = None,
    ) -> sqlite3.Row:
        artifact_id = require_identifier(artifact_id or generated_id("artifact"), "artifact_id")
        destination, relative, digest = self._write_artifact_bytes(
            artifact_id=artifact_id,
            kind=kind,
            raw=raw,
            suffix=suffix,
        )
        try:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, run_id, kind, relative_path, sha256, byte_count,
                    char_count, content_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    kind,
                    relative,
                    digest,
                    len(raw),
                    char_count,
                    content_type,
                    utc_now(),
                ),
            )
        except Exception:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise
        return self._artifact_row(connection, artifact_id)

    def _store_text_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        text: str,
        artifact_id: str | None = None,
    ) -> sqlite3.Row:
        if not text:
            raise ResearchError(f"{kind} content must not be empty")
        return self._store_artifact(
            connection,
            run_id=run_id,
            kind=kind,
            raw=text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            char_count=len(text),
            suffix=".txt",
            artifact_id=artifact_id,
        )

    def _source_row(self, connection: sqlite3.Connection, source_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise ResearchError(f"unknown source: {source_id}")
        return row

    def _shard_row(self, connection: sqlite3.Connection, shard_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT shards.*, sources.run_id, sources.text_chars AS source_text_chars
            FROM shards
            JOIN sources ON sources.id = shards.source_id
            WHERE shards.id = ?
            """,
            (shard_id,),
        ).fetchone()
        if row is None:
            raise ResearchError(f"unknown shard: {shard_id}")
        return row

    def _summary_row(self, connection: sqlite3.Connection, summary_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT summaries.*, sources.run_id
            FROM summaries
            JOIN shards ON shards.id = summaries.shard_id
            JOIN sources ON sources.id = shards.source_id
            WHERE summaries.id = ?
            """,
            (summary_id,),
        ).fetchone()
        if row is None:
            raise ResearchError(f"unknown summary: {summary_id}")
        return row

    def _assert_evidence_for_run(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        summary_ids: Iterable[str],
        shard_ids: Iterable[str],
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for summary_id in dict.fromkeys(summary_ids):
            row = self._summary_row(connection, require_identifier(summary_id, "summary_id"))
            if row["run_id"] != run_id:
                raise ResearchError(f"summary does not belong to run {run_id}: {summary_id}")
            entries.append(("summary", summary_id))
        for shard_id in dict.fromkeys(shard_ids):
            row = self._shard_row(connection, require_identifier(shard_id, "shard_id"))
            if row["run_id"] != run_id:
                raise ResearchError(f"shard does not belong to run {run_id}: {shard_id}")
            entries.append(("shard", shard_id))
        if not entries:
            raise ResearchError("at least one summary or shard evidence reference is required")
        return entries

    def create_run(
        self,
        *,
        title: str,
        question: str,
        created_by: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        run_id = require_identifier(run_id or generated_id("research"), "run_id")
        title = require_nonempty(title, "title")
        created_by = require_nonempty(created_by, "created_by")
        if not question:
            raise ResearchError("question must not be empty")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO research_runs(
                        id, title, question_artifact_id, created_by, status, created_at
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?)
                    """,
                    (run_id, title, "PENDING", created_by, utc_now()),
                )
                question_artifact = self._store_text_artifact(
                    connection,
                    run_id=run_id,
                    kind="research-brief",
                    text=question,
                )
                connection.execute(
                    "UPDATE research_runs SET question_artifact_id = ? WHERE id = ?",
                    (question_artifact["id"], run_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            row = self._run_row(connection, run_id)
            return {
                "status": "created",
                "run": {
                    "run_id": row["id"],
                    "title": row["title"],
                    "created_by": row["created_by"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "question": self._artifact_ref(question_artifact),
                },
            }

    def add_file(
        self,
        *,
        run_id: str,
        path_value: str,
        source_id: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        run_id = require_identifier(run_id, "run_id")
        source_id = require_identifier(source_id or generated_id("source"), "source_id")
        source_path = Path(path_value).expanduser().resolve()
        if not source_path.is_file():
            raise ResearchError(f"source file does not exist: {source_path}")
        raw = source_path.read_bytes()
        if not raw:
            raise ResearchError("source file must not be empty")
        resolved_type = content_type or mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_run(connection, run_id)
                if connection.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
                    raise ResearchError(f"source identifier already exists: {source_id}")
                raw_artifact = self._store_artifact(
                    connection,
                    run_id=run_id,
                    kind="raw-source",
                    raw=raw,
                    content_type=resolved_type,
                    char_count=None,
                    suffix=".bin",
                )
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, run_id, source_type, origin, raw_artifact_id, content_type, created_at
                    ) VALUES (?, ?, 'file', ?, ?, ?, ?)
                    """,
                    (source_id, run_id, str(source_path), raw_artifact["id"], resolved_type, utc_now()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "source_added",
            "source": {
                "source_id": source_id,
                "run_id": run_id,
                "source_type": "file",
                "origin": str(source_path),
                "content_type": resolved_type,
                "raw_artifact": self._artifact_ref(raw_artifact),
            },
        }

    def add_url(
        self,
        *,
        run_id: str,
        url: str,
        source_id: str | None = None,
        timeout_seconds: int = 30,
        max_bytes: int = 50_000_000,
    ) -> dict[str, Any]:
        self._require_initialized()
        run_id = require_identifier(run_id, "run_id")
        source_id = require_identifier(source_id or generated_id("source"), "source_id")
        if timeout_seconds <= 0:
            raise ResearchError("--timeout-seconds must be positive")
        if max_bytes <= 0:
            raise ResearchError("--max-bytes must be positive")
        safe_origin = redact_url(url)
        request = Request(url, headers={"User-Agent": "agent-team-research/1.0"})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(65_536, max_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResearchError(f"download exceeds --max-bytes ({max_bytes})")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                content_type = response.headers.get_content_type() or "application/octet-stream"
                charset = response.headers.get_content_charset()
                if charset:
                    content_type = f"{content_type}; charset={charset}"
                final_origin = redact_url(response.geturl())
        except HTTPError as exc:
            raise ResearchError(f"URL retrieval failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise ResearchError(f"URL retrieval failed: {exc.reason}") from exc
        if not raw:
            raise ResearchError("URL response body is empty")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_run(connection, run_id)
                if connection.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
                    raise ResearchError(f"source identifier already exists: {source_id}")
                raw_artifact = self._store_artifact(
                    connection,
                    run_id=run_id,
                    kind="raw-source",
                    raw=raw,
                    content_type=content_type,
                    char_count=None,
                    suffix=".bin",
                )
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, run_id, source_type, origin, raw_artifact_id, content_type, created_at
                    ) VALUES (?, ?, 'url', ?, ?, ?, ?)
                    """,
                    (source_id, run_id, final_origin or safe_origin, raw_artifact["id"], content_type, utc_now()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "source_added",
            "source": {
                "source_id": source_id,
                "run_id": run_id,
                "source_type": "url",
                "origin": final_origin or safe_origin,
                "content_type": content_type,
                "raw_artifact": self._artifact_ref(raw_artifact),
            },
        }

    def shard_source(
        self,
        *,
        source_id: str,
        max_chars: int,
        overlap_chars: int,
        encoding: str,
    ) -> dict[str, Any]:
        self._require_initialized()
        source_id = require_identifier(source_id, "source_id")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = self._source_row(connection, source_id)
                self._require_active_run(connection, source["run_id"])
                existing = connection.execute(
                    "SELECT COUNT(*) AS count FROM shards WHERE source_id = ?", (source_id,)
                ).fetchone()["count"]
                if existing:
                    raise ResearchError(
                        f"source already has {existing} shard(s); preserve them as immutable evidence"
                    )
                raw_artifact = self._artifact_row(connection, source["raw_artifact_id"])
                raw_path = (self.artifact_root / raw_artifact["relative_path"]).resolve()
                try:
                    raw_path.relative_to(self.artifact_root)
                except ValueError as exc:
                    raise ResearchError("raw artifact path escapes artifact root") from exc
                if not raw_path.is_file():
                    raise ResearchError(f"raw artifact is missing: {raw_artifact['id']}")
                raw = raw_path.read_bytes()
                if sha256_bytes(raw) != raw_artifact["sha256"]:
                    raise ResearchError(f"raw artifact hash mismatch: {raw_artifact['id']}")
                text = decode_source_text(raw, encoding=encoding)
                chunks = split_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
                normalized = self._store_text_artifact(
                    connection,
                    run_id=source["run_id"],
                    kind="normalized-text",
                    text=text,
                )
                rows: list[dict[str, Any]] = []
                for ordinal, (start, end, chunk) in enumerate(chunks):
                    artifact = self._store_text_artifact(
                        connection,
                        run_id=source["run_id"],
                        kind="source-shard",
                        text=chunk,
                    )
                    shard_id = generated_id("shard")
                    connection.execute(
                        """
                        INSERT INTO shards(id, source_id, ordinal, char_start, char_end, artifact_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (shard_id, source_id, ordinal, start, end, artifact["id"], utc_now()),
                    )
                    rows.append(
                        {
                            "shard_id": shard_id,
                            "ordinal": ordinal,
                            "char_start": start,
                            "char_end": end,
                            "char_count": end - start,
                            "artifact": self._artifact_ref(artifact),
                        }
                    )
                connection.execute(
                    """
                    UPDATE sources
                    SET normalized_text_artifact_id = ?, text_chars = ?
                    WHERE id = ?
                    """,
                    (normalized["id"], len(text), source_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "source_sharded",
            "source_id": source_id,
            "text_chars": len(text),
            "normalized_text": self._artifact_ref(normalized),
            "shard_count": len(rows),
            "shards": rows,
        }

    def record_summary(
        self,
        *,
        shard_id: str,
        summary: str,
        advisory_absolute_limit: int,
        summary_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        shard_id = require_identifier(shard_id, "shard_id")
        summary_id = require_identifier(summary_id or generated_id("summary"), "summary_id")
        if advisory_absolute_limit <= 0:
            raise ResearchError("--advisory-absolute-limit must be positive")
        if not summary:
            raise ResearchError("summary must not be empty")
        actual_chars = len(summary)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                shard = self._shard_row(connection, shard_id)
                self._require_active_run(connection, shard["run_id"])
                shard_chars = shard["char_end"] - shard["char_start"]
                if shard_chars <= 0:
                    raise ResearchError("shard has invalid character bounds")
                if connection.execute(
                    "SELECT 1 FROM summaries WHERE shard_id = ?", (shard_id,)
                ).fetchone():
                    raise ResearchError(
                        f"shard already has an immutable summary: {shard_id}"
                    )
                target_chars = int(shard_chars * 0.10)
                actual_ratio = actual_chars / shard_chars
                over_target = actual_ratio > 0.10
                over_advisory_absolute_limit = actual_chars > advisory_absolute_limit
                artifact = self._store_text_artifact(
                    connection,
                    run_id=shard["run_id"],
                    kind="shard-summary",
                    text=summary,
                )
                connection.execute(
                    """
                    INSERT INTO summaries(
                        id, shard_id, artifact_id, target_chars, actual_chars,
                        actual_ratio, advisory_absolute_limit, over_target,
                        over_advisory_absolute_limit, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id,
                        shard_id,
                        artifact["id"],
                        target_chars,
                        actual_chars,
                        actual_ratio,
                        advisory_absolute_limit,
                        1 if over_target else 0,
                        1 if over_advisory_absolute_limit else 0,
                        utc_now(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "summary_recorded",
            "summary": {
                "summary_id": summary_id,
                "shard_id": shard_id,
                "artifact": self._artifact_ref(artifact),
                "target_ratio": 0.10,
                "target_chars": target_chars,
                "actual_chars": actual_chars,
                "actual_ratio": actual_ratio,
                "advisory_absolute_limit": advisory_absolute_limit,
                "over_target_warning": over_target,
                "over_advisory_absolute_limit_warning": over_advisory_absolute_limit,
                "policy": "The 10% and advisory size targets are metadata only. The summary is retained intact without truncation or rejection.",
            },
        }

    def add_claim(
        self,
        *,
        run_id: str,
        statement: str,
        confidence: str,
        summary_ids: Sequence[str],
        shard_ids: Sequence[str],
        claim_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        run_id = require_identifier(run_id, "run_id")
        claim_id = require_identifier(claim_id or generated_id("claim"), "claim_id")
        if confidence not in CONFIDENCE_LEVELS:
            raise ResearchError(f"confidence must be one of: {', '.join(sorted(CONFIDENCE_LEVELS))}")
        if not statement:
            raise ResearchError("claim statement must not be empty")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_run(connection, run_id)
                if connection.execute("SELECT 1 FROM claims WHERE id = ?", (claim_id,)).fetchone():
                    raise ResearchError(f"claim identifier already exists: {claim_id}")
                evidence = self._assert_evidence_for_run(
                    connection,
                    run_id=run_id,
                    summary_ids=summary_ids,
                    shard_ids=shard_ids,
                )
                statement_artifact = self._store_text_artifact(
                    connection,
                    run_id=run_id,
                    kind="claim-statement",
                    text=statement,
                )
                connection.execute(
                    """
                    INSERT INTO claims(id, run_id, statement_artifact_id, confidence, status, created_at)
                    VALUES (?, ?, ?, ?, 'ACTIVE', ?)
                    """,
                    (claim_id, run_id, statement_artifact["id"], confidence, utc_now()),
                )
                connection.executemany(
                    "INSERT INTO claim_evidence(claim_id, evidence_kind, evidence_id) VALUES (?, ?, ?)",
                    [(claim_id, kind, evidence_id) for kind, evidence_id in evidence],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "claim_added",
            "claim": {
                "claim_id": claim_id,
                "run_id": run_id,
                "confidence": confidence,
                "statement": self._artifact_ref(statement_artifact),
                "evidence": [
                    {"kind": kind, "id": evidence_id} for kind, evidence_id in evidence
                ],
            },
        }

    def open_conflict(
        self,
        *,
        run_id: str,
        claim_id: str,
        description: str,
        supporting_summary_ids: Sequence[str],
        supporting_shard_ids: Sequence[str],
        contradicting_summary_ids: Sequence[str],
        contradicting_shard_ids: Sequence[str],
        conflict_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        run_id = require_identifier(run_id, "run_id")
        claim_id = require_identifier(claim_id, "claim_id")
        conflict_id = require_identifier(conflict_id or generated_id("conflict"), "conflict_id")
        if not description:
            raise ResearchError("conflict description must not be empty")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_run(connection, run_id)
                claim = connection.execute(
                    "SELECT * FROM claims WHERE id = ?", (claim_id,)
                ).fetchone()
                if claim is None or claim["run_id"] != run_id:
                    raise ResearchError(f"claim does not belong to run {run_id}: {claim_id}")
                if connection.execute("SELECT 1 FROM conflicts WHERE id = ?", (conflict_id,)).fetchone():
                    raise ResearchError(f"conflict identifier already exists: {conflict_id}")
                supporting = self._assert_evidence_for_run(
                    connection,
                    run_id=run_id,
                    summary_ids=supporting_summary_ids,
                    shard_ids=supporting_shard_ids,
                )
                contradicting = self._assert_evidence_for_run(
                    connection,
                    run_id=run_id,
                    summary_ids=contradicting_summary_ids,
                    shard_ids=contradicting_shard_ids,
                )
                overlap = set(supporting).intersection(contradicting)
                if overlap:
                    rendered = ", ".join(f"{kind}:{evidence_id}" for kind, evidence_id in sorted(overlap))
                    raise ResearchError(
                        "supporting and contradicting evidence must be distinct: " + rendered
                    )
                description_artifact = self._store_text_artifact(
                    connection,
                    run_id=run_id,
                    kind="conflict-description",
                    text=description,
                )
                connection.execute(
                    """
                    INSERT INTO conflicts(
                        id, run_id, claim_id, description_artifact_id, status, opened_at
                    ) VALUES (?, ?, ?, ?, 'OPEN', ?)
                    """,
                    (conflict_id, run_id, claim_id, description_artifact["id"], utc_now()),
                )
                values = [
                    (conflict_id, "supporting", kind, evidence_id)
                    for kind, evidence_id in supporting
                ] + [
                    (conflict_id, "contradicting", kind, evidence_id)
                    for kind, evidence_id in contradicting
                ]
                connection.executemany(
                    """
                    INSERT INTO conflict_evidence(conflict_id, side, evidence_kind, evidence_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    values,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "conflict_opened",
            "conflict": {
                "conflict_id": conflict_id,
                "run_id": run_id,
                "claim_id": claim_id,
                "description": self._artifact_ref(description_artifact),
                "status": "OPEN",
                "supporting_evidence": [
                    {"kind": kind, "id": evidence_id} for kind, evidence_id in supporting
                ],
                "contradicting_evidence": [
                    {"kind": kind, "id": evidence_id} for kind, evidence_id in contradicting
                ],
            },
        }

    def resolve_conflict(
        self,
        *,
        conflict_id: str,
        status: str,
        rationale: str,
        summary_ids: Sequence[str],
        shard_ids: Sequence[str],
    ) -> dict[str, Any]:
        self._require_initialized()
        conflict_id = require_identifier(conflict_id, "conflict_id")
        if status not in {"RESOLVED", "QUALIFIED", "UNRESOLVED"}:
            raise ResearchError("resolution status must be RESOLVED, QUALIFIED, or UNRESOLVED")
        if not rationale:
            raise ResearchError("resolution rationale must not be empty")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                conflict = connection.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
                if conflict is None:
                    raise ResearchError(f"unknown conflict: {conflict_id}")
                if conflict["status"] != "OPEN":
                    raise ResearchError(f"conflict is not open: {conflict_id}")
                self._require_active_run(connection, conflict["run_id"])
                evidence = self._assert_evidence_for_run(
                    connection,
                    run_id=conflict["run_id"],
                    summary_ids=summary_ids,
                    shard_ids=shard_ids,
                )
                resolution_artifact = self._store_text_artifact(
                    connection,
                    run_id=conflict["run_id"],
                    kind="conflict-resolution",
                    text=rationale,
                )
                connection.execute(
                    """
                    UPDATE conflicts
                    SET status = ?, resolution_artifact_id = ?, resolved_at = ?
                    WHERE id = ?
                    """,
                    (status, resolution_artifact["id"], utc_now(), conflict_id),
                )
                connection.executemany(
                    """
                    INSERT INTO conflict_evidence(conflict_id, side, evidence_kind, evidence_id)
                    VALUES (?, 'resolution', ?, ?)
                    """,
                    [(conflict_id, kind, evidence_id) for kind, evidence_id in evidence],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "conflict_resolved",
            "conflict": {
                "conflict_id": conflict_id,
                "resolution_status": status,
                "rationale": self._artifact_ref(resolution_artifact),
                "resolution_evidence": [
                    {"kind": kind, "id": evidence_id} for kind, evidence_id in evidence
                ],
            },
        }

    def finalize(self, *, run_id: str, conclusion: str) -> dict[str, Any]:
        self._require_initialized()
        run_id = require_identifier(run_id, "run_id")
        if not conclusion:
            raise ResearchError("conclusion must not be empty")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_run(connection, run_id)
                summary_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM summaries
                    JOIN shards ON shards.id = summaries.shard_id
                    JOIN sources ON sources.id = shards.source_id
                    WHERE sources.run_id = ?
                    """,
                    (run_id,),
                ).fetchone()["count"]
                claim_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM claims WHERE run_id = ? AND status = 'ACTIVE'",
                    (run_id,),
                ).fetchone()["count"]
                if summary_count == 0:
                    raise ResearchError("cannot finalize without at least one recorded shard summary")
                if claim_count == 0:
                    raise ResearchError("cannot finalize without at least one evidence-backed claim")
                open_conflicts = connection.execute(
                    "SELECT COUNT(*) AS count FROM conflicts WHERE run_id = ? AND status = 'OPEN'",
                    (run_id,),
                ).fetchone()["count"]
                if open_conflicts:
                    raise ResearchError(
                        f"cannot finalize with {open_conflicts} OPEN conflict(s); resolve or mark them UNRESOLVED first"
                    )
                unresolved_conflicts = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM conflicts
                    WHERE run_id = ? AND status IN ('QUALIFIED', 'UNRESOLVED')
                    """,
                    (run_id,),
                ).fetchone()["count"]
                conclusion_artifact = self._store_text_artifact(
                    connection,
                    run_id=run_id,
                    kind="final-conclusion",
                    text=conclusion,
                )
                metadata = {
                    "summary_count": summary_count,
                    "claim_count": claim_count,
                    "unresolved_or_qualified_conflict_count": unresolved_conflicts,
                    "warning": (
                        "Final conclusion retains qualified or unresolved conflicts."
                        if unresolved_conflicts
                        else None
                    ),
                }
                connection.execute(
                    """
                    UPDATE research_runs
                    SET status = 'FINALIZED', finalized_at = ?, conclusion_artifact_id = ?, finalization_json = ?
                    WHERE id = ?
                    """,
                    (utc_now(), conclusion_artifact["id"], compact_json(metadata), run_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "finalized",
            "run_id": run_id,
            "conclusion": self._artifact_ref(conclusion_artifact),
            "finalization": metadata,
        }

    def select_context(
        self,
        *,
        run_id: str,
        role: str,
        max_content_chars: int,
        max_artifacts: int,
        claim_ids: Sequence[str],
        source_ids: Sequence[str],
        include_conflicts: bool,
        include_raw_shards: bool,
    ) -> dict[str, Any]:
        """Return references only; materialization is a separate, local action."""

        self._require_initialized()
        run_id = require_identifier(run_id, "run_id")
        role = require_nonempty(role, "role")
        if max_content_chars <= 0:
            raise ResearchError("--max-content-chars must be positive")
        if max_artifacts <= 0:
            raise ResearchError("--max-artifacts must be positive")

        with closing(self._connect()) as connection:
            run = self._run_row(connection, run_id)
            requested_claims = list(dict.fromkeys(claim_ids))
            requested_sources = list(dict.fromkeys(source_ids))
            for claim_id in requested_claims:
                row = connection.execute("SELECT run_id FROM claims WHERE id = ?", (claim_id,)).fetchone()
                if row is None or row["run_id"] != run_id:
                    raise ResearchError(f"claim does not belong to run {run_id}: {claim_id}")
            for source_id in requested_sources:
                source = self._source_row(connection, source_id)
                if source["run_id"] != run_id:
                    raise ResearchError(f"source does not belong to run {run_id}: {source_id}")

            candidates: list[tuple[int, str, sqlite3.Row, str]] = []
            seen_artifacts: set[str] = set()

            def add_candidate(priority: int, artifact_id: str | None, reason: str) -> None:
                if not artifact_id or artifact_id in seen_artifacts:
                    return
                artifact = self._artifact_row(connection, artifact_id)
                if artifact["run_id"] != run_id:
                    raise ResearchError(f"artifact does not belong to run {run_id}: {artifact_id}")
                seen_artifacts.add(artifact_id)
                candidates.append((priority, artifact_id, artifact, reason))

            # The brief is essential but compact, so it is first in every research packet.
            add_candidate(0, run["question_artifact_id"], "research_brief")
            if run["conclusion_artifact_id"]:
                add_candidate(1, run["conclusion_artifact_id"], "final_conclusion")

            claim_filter = requested_claims or [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM claims WHERE run_id = ? AND status = 'ACTIVE' ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            ]
            for claim_id in claim_filter:
                claim = connection.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
                if claim is None:
                    continue
                add_candidate(10, claim["statement_artifact_id"], f"claim:{claim_id}")
                evidence_rows = connection.execute(
                    """
                    SELECT evidence_kind, evidence_id
                    FROM claim_evidence
                    WHERE claim_id = ?
                    ORDER BY evidence_kind, evidence_id
                    """,
                    (claim_id,),
                ).fetchall()
                for evidence in evidence_rows:
                    if evidence["evidence_kind"] == "summary":
                        summary = self._summary_row(connection, evidence["evidence_id"])
                        add_candidate(20, summary["artifact_id"], f"claim_evidence:{claim_id}")
                    elif include_raw_shards:
                        shard = self._shard_row(connection, evidence["evidence_id"])
                        add_candidate(40, shard["artifact_id"], f"claim_raw_evidence:{claim_id}")

            for source_id in requested_sources:
                summary_rows = connection.execute(
                    """
                    SELECT summaries.artifact_id
                    FROM summaries
                    JOIN shards ON shards.id = summaries.shard_id
                    WHERE shards.source_id = ?
                    ORDER BY shards.ordinal
                    """,
                    (source_id,),
                ).fetchall()
                for summary in summary_rows:
                    add_candidate(30, summary["artifact_id"], f"source_summary:{source_id}")
                if include_raw_shards:
                    shard_rows = connection.execute(
                        "SELECT artifact_id FROM shards WHERE source_id = ? ORDER BY ordinal",
                        (source_id,),
                    ).fetchall()
                    for shard in shard_rows:
                        add_candidate(45, shard["artifact_id"], f"source_raw_shard:{source_id}")

            conflict_rows: list[sqlite3.Row] = []
            if include_conflicts:
                conflict_rows = connection.execute(
                    """
                    SELECT * FROM conflicts
                    WHERE run_id = ?
                    ORDER BY CASE status WHEN 'OPEN' THEN 0 ELSE 1 END, opened_at
                    """,
                    (run_id,),
                ).fetchall()
                for conflict in conflict_rows:
                    add_candidate(5, conflict["description_artifact_id"], f"conflict:{conflict['id']}")
                    add_candidate(6, conflict["resolution_artifact_id"], f"conflict_resolution:{conflict['id']}")

            candidates.sort(key=lambda item: (item[0], item[1]))
            selected: list[dict[str, Any]] = []
            omitted: list[dict[str, Any]] = []
            used_chars = 0
            for _, artifact_id, artifact, reason in candidates:
                chars = artifact["char_count"]
                if chars is None:
                    omitted.append(
                        {
                            "artifact_id": artifact_id,
                            "reason": "binary_or_unbounded_artifact",
                        }
                    )
                    continue
                if len(selected) >= max_artifacts:
                    omitted.append(
                        {"artifact_id": artifact_id, "reason": "max_artifacts"}
                    )
                    continue
                if used_chars + chars > max_content_chars:
                    omitted.append(
                        {
                            "artifact_id": artifact_id,
                            "reason": "max_content_chars",
                            "artifact_chars": chars,
                            "remaining_chars": max_content_chars - used_chars,
                        }
                    )
                    continue
                used_chars += chars
                selected.append({"reason": reason, "artifact": self._artifact_ref(artifact)})

            claims = [
                {
                    "claim_id": row["id"],
                    "confidence": row["confidence"],
                    "statement_artifact_id": row["statement_artifact_id"],
                }
                for row in connection.execute(
                    "SELECT * FROM claims WHERE run_id = ? AND status = 'ACTIVE' ORDER BY created_at",
                    (run_id,),
                ).fetchall()
                if not requested_claims or row["id"] in requested_claims
            ]
            conflicts = [
                {
                    "conflict_id": row["id"],
                    "claim_id": row["claim_id"],
                    "status": row["status"],
                    "description_artifact_id": row["description_artifact_id"],
                    "resolution_artifact_id": row["resolution_artifact_id"],
                }
                for row in conflict_rows
            ]
            return {
                "status": "context_selected",
                "research_run": {
                    "run_id": run["id"],
                    "title": run["title"],
                    "status": run["status"],
                },
                "target_role": role,
                "selection_contract": {
                    "raw_content_included": False,
                    "queue_safe": True,
                    "materialization_required": "Read only selected artifact references from the local artifact root after hash verification.",
                    "max_content_chars": max_content_chars,
                    "selected_content_chars": used_chars,
                    "max_artifacts": max_artifacts,
                },
                "selected_artifacts": selected,
                "context_compiler_artifact_paths": [
                    entry["artifact"]["relative_path"] for entry in selected
                ],
                "claims": claims,
                "conflicts": conflicts,
                "omitted": omitted,
            }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local SQLite research ledger with local artifact storage and reference-only context selection."
    )
    parser.add_argument("--db", required=True, help="SQLite research-ledger path")
    parser.add_argument("--artifact-root", required=True, help="Local directory for raw and derived artifacts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or validate the local research ledger")

    create = subparsers.add_parser("create-run", help="Create an active research run")
    create.add_argument("--run-id")
    create.add_argument("--title", required=True)
    create.add_argument("--created-by", default="pl")
    create_question = create.add_mutually_exclusive_group(required=True)
    create_question.add_argument("--question")
    create_question.add_argument("--question-file")

    add_file = subparsers.add_parser("add-file", help="Copy one local source file into the artifact store")
    add_file.add_argument("--run-id", required=True)
    add_file.add_argument("--source-id")
    add_file.add_argument("--path", required=True)
    add_file.add_argument("--content-type")

    add_url = subparsers.add_parser("add-url", help="Retrieve one HTTP(S) source into the artifact store")
    add_url.add_argument("--run-id", required=True)
    add_url.add_argument("--source-id")
    add_url.add_argument("--url", required=True)
    add_url.add_argument("--timeout-seconds", type=int, default=30)
    add_url.add_argument("--max-bytes", type=int, default=50_000_000)

    shard = subparsers.add_parser("shard-source", help="Decode and split a source into immutable text shards")
    shard.add_argument("--source-id", required=True)
    shard.add_argument("--max-chars", type=int, default=20_000)
    shard.add_argument("--overlap-chars", type=int, default=0)
    shard.add_argument("--encoding", default="auto")

    summary = subparsers.add_parser(
        "record-summary",
        help="Store one intact summary; size targets are advisory and never truncate or reject it",
    )
    summary.add_argument("--summary-id")
    summary.add_argument("--shard-id", required=True)
    summary_input = summary.add_mutually_exclusive_group(required=True)
    summary_input.add_argument("--summary")
    summary_input.add_argument("--summary-file")
    summary.add_argument(
        "--advisory-absolute-limit",
        "--absolute-limit",
        dest="advisory_absolute_limit",
        type=int,
        default=20_000,
        help="Metadata-only size warning threshold; no ledger rejection or truncation occurs",
    )

    claim = subparsers.add_parser("add-claim", help="Add a concise, evidence-backed claim")
    claim.add_argument("--claim-id")
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--confidence", required=True, choices=sorted(CONFIDENCE_LEVELS))
    claim_input = claim.add_mutually_exclusive_group(required=True)
    claim_input.add_argument("--statement")
    claim_input.add_argument("--statement-file")
    claim.add_argument("--summary-id", action="append", default=[])
    claim.add_argument("--shard-id", action="append", default=[])

    conflict = subparsers.add_parser("open-conflict", help="Open an evidence-backed conflict for one claim")
    conflict.add_argument("--conflict-id")
    conflict.add_argument("--run-id", required=True)
    conflict.add_argument("--claim-id", required=True)
    conflict_input = conflict.add_mutually_exclusive_group(required=True)
    conflict_input.add_argument("--description")
    conflict_input.add_argument("--description-file")
    conflict.add_argument("--support-summary-id", action="append", default=[])
    conflict.add_argument("--support-shard-id", action="append", default=[])
    conflict.add_argument("--contradict-summary-id", action="append", default=[])
    conflict.add_argument("--contradict-shard-id", action="append", default=[])

    resolve = subparsers.add_parser("resolve-conflict", help="Resolve, qualify, or preserve one conflict")
    resolve.add_argument("--conflict-id", required=True)
    resolve.add_argument("--status", required=True, choices=["RESOLVED", "QUALIFIED", "UNRESOLVED"])
    rationale = resolve.add_mutually_exclusive_group(required=True)
    rationale.add_argument("--rationale")
    rationale.add_argument("--rationale-file")
    resolve.add_argument("--summary-id", action="append", default=[])
    resolve.add_argument("--shard-id", action="append", default=[])

    finalize = subparsers.add_parser("finalize", help="Persist the final conclusion after all open conflicts are closed")
    finalize.add_argument("--run-id", required=True)
    conclusion = finalize.add_mutually_exclusive_group(required=True)
    conclusion.add_argument("--conclusion")
    conclusion.add_argument("--conclusion-file")

    select_context = subparsers.add_parser(
        "select-context",
        help="Select bounded artifact references only; it does not print material content",
    )
    select_context.add_argument("--run-id", required=True)
    select_context.add_argument("--role", required=True)
    select_context.add_argument("--max-content-chars", type=int, default=40_000)
    select_context.add_argument("--max-artifacts", type=int, default=24)
    select_context.add_argument("--claim-id", action="append", default=[])
    select_context.add_argument("--source-id", action="append", default=[])
    select_context.add_argument("--include-conflicts", action="store_true")
    select_context.add_argument("--include-raw-shards", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ledger = ResearchLedger(args.db, args.artifact_root)
    try:
        if args.command == "init":
            result = ledger.initialize()
        elif args.command == "create-run":
            result = ledger.create_run(
                run_id=args.run_id,
                title=args.title,
                question=read_text_argument(
                    inline=args.question, file_path=args.question_file, label="question"
                ),
                created_by=args.created_by,
            )
        elif args.command == "add-file":
            result = ledger.add_file(
                run_id=args.run_id,
                source_id=args.source_id,
                path_value=args.path,
                content_type=args.content_type,
            )
        elif args.command == "add-url":
            result = ledger.add_url(
                run_id=args.run_id,
                source_id=args.source_id,
                url=args.url,
                timeout_seconds=args.timeout_seconds,
                max_bytes=args.max_bytes,
            )
        elif args.command == "shard-source":
            result = ledger.shard_source(
                source_id=args.source_id,
                max_chars=args.max_chars,
                overlap_chars=args.overlap_chars,
                encoding=args.encoding,
            )
        elif args.command == "record-summary":
            result = ledger.record_summary(
                summary_id=args.summary_id,
                shard_id=args.shard_id,
                summary=read_text_argument(
                    inline=args.summary, file_path=args.summary_file, label="summary"
                ),
                advisory_absolute_limit=args.advisory_absolute_limit,
            )
        elif args.command == "add-claim":
            result = ledger.add_claim(
                claim_id=args.claim_id,
                run_id=args.run_id,
                statement=read_text_argument(
                    inline=args.statement, file_path=args.statement_file, label="statement"
                ),
                confidence=args.confidence,
                summary_ids=args.summary_id,
                shard_ids=args.shard_id,
            )
        elif args.command == "open-conflict":
            result = ledger.open_conflict(
                conflict_id=args.conflict_id,
                run_id=args.run_id,
                claim_id=args.claim_id,
                description=read_text_argument(
                    inline=args.description,
                    file_path=args.description_file,
                    label="description",
                ),
                supporting_summary_ids=args.support_summary_id,
                supporting_shard_ids=args.support_shard_id,
                contradicting_summary_ids=args.contradict_summary_id,
                contradicting_shard_ids=args.contradict_shard_id,
            )
        elif args.command == "resolve-conflict":
            result = ledger.resolve_conflict(
                conflict_id=args.conflict_id,
                status=args.status,
                rationale=read_text_argument(
                    inline=args.rationale, file_path=args.rationale_file, label="rationale"
                ),
                summary_ids=args.summary_id,
                shard_ids=args.shard_id,
            )
        elif args.command == "finalize":
            result = ledger.finalize(
                run_id=args.run_id,
                conclusion=read_text_argument(
                    inline=args.conclusion,
                    file_path=args.conclusion_file,
                    label="conclusion",
                ),
            )
        elif args.command == "select-context":
            result = ledger.select_context(
                run_id=args.run_id,
                role=args.role,
                max_content_chars=args.max_content_chars,
                max_artifacts=args.max_artifacts,
                claim_ids=args.claim_id,
                source_ids=args.source_id,
                include_conflicts=args.include_conflicts,
                include_raw_shards=args.include_raw_shards,
            )
        else:  # pragma: no cover - argparse makes this unreachable.
            raise AssertionError(args.command)
    except (ResearchError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
