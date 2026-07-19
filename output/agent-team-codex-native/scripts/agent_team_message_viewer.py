from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

try:
    from .agent_team_context import GitContextReader, RepositoryRegistry
    from .agent_team_queue import Message, SQLiteMessageQueue
except ImportError:
    from agent_team_context import GitContextReader, RepositoryRegistry
    from agent_team_queue import Message, SQLiteMessageQueue


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / ".agent-team" / "state" / "agent-team.db"


def git_summary(
    message: Message,
    *,
    registry: RepositoryRegistry | None,
    show_diff: bool,
    max_diff_chars: int,
) -> dict[str, Any] | None:
    payload = message.payload
    repo_id = payload.get("repo_id")
    base_oid = payload.get("base_oid")
    head_oid = payload.get("head_oid")
    if not all(isinstance(value, str) and value for value in (repo_id, base_oid, head_oid)):
        return None
    if registry is None:
        return {
            "repo_id": repo_id,
            "base_oid": base_oid,
            "head_oid": head_oid,
            "unresolved": "repository registry was not supplied",
        }

    reader = GitContextReader(registry.get(repo_id))
    verified_base = reader.verify_commit(base_oid)
    verified_head = reader.verify_commit(head_oid)
    summary: dict[str, Any] = {
        "repo_id": repo_id,
        "base_oid": verified_base,
        "head_oid": verified_head,
        "changed_paths": reader.changed_paths(verified_base, verified_head),
        "diff_stat": reader.diff_stat(verified_base, verified_head).rstrip(),
        "commit_series": reader.commit_series(verified_base, verified_head),
    }
    if show_diff:
        diff, truncated = reader.diff_text(
            verified_base,
            verified_head,
            max_chars=max_diff_chars,
        )
        summary["diff"] = diff
        summary["diff_truncated"] = truncated
    return summary


def human_view(message: Message, git: dict[str, Any] | None) -> str:
    lines = [
        "=" * 88,
        f"Message #{message.seq}  {message.id}",
        f"Thread     : {message.thread_id}",
        f"Work item  : {message.work_item_id}",
        f"Route      : {message.from_role} -> {message.to_role}",
        f"Type       : {message.type}",
        f"Status     : {message.status}",
        f"Created    : {message.created_at}",
        f"Priority   : {message.priority}",
        "Payload:",
        json.dumps(message.payload, ensure_ascii=False, indent=2),
    ]
    if git is not None:
        lines.extend(
            [
                "Git change:",
                f"  repository : {git.get('repo_id')}",
                f"  base       : {git.get('base_oid')}",
                f"  head       : {git.get('head_oid')}",
            ]
        )
        if git.get("unresolved"):
            lines.append(f"  unresolved : {git['unresolved']}")
        else:
            lines.append("  changed paths:")
            for change in git.get("changed_paths", []):
                old_path = (
                    f"{change['old_path']} -> " if change.get("old_path") else ""
                )
                lines.append(
                    f"    {change.get('status', '?'):>4}  {old_path}{change.get('path')}"
                )
            lines.extend(["  diff stat:", str(git.get("diff_stat", ""))])
            lines.append("  commits:")
            for commit in git.get("commit_series", []):
                lines.append(f"    {commit['oid'][:12]}  {commit['subject']}")
            if "diff" in git:
                lines.extend(["  diff:", git["diff"]])
                if git.get("diff_truncated"):
                    lines.append("  [diff truncated]")
    return "\n".join(lines)


def render_message(
    message: Message,
    *,
    registry: RepositoryRegistry | None,
    show_diff: bool,
    max_diff_chars: int,
    as_json: bool,
) -> str:
    git = git_summary(
        message,
        registry=registry,
        show_diff=show_diff,
        max_diff_chars=max_diff_chars,
    )
    if as_json:
        return json.dumps(
            {"message": asdict(message), "git": git},
            ensure_ascii=False,
            indent=2,
        )
    return human_view(message, git)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Human-only viewer for SQLite agent messages and Git OIDs"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Message database (default: .agent-team/state/agent-team.db).",
    )
    parser.add_argument("--registry", help="Repository registry for optional Git resolution.")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--message-id", help="Show one specific message.")
    selector.add_argument("--thread", help="Optional thread filter.")
    parser.add_argument("--role", help="Optional recipient-role filter.")
    parser.add_argument("--status", help="Optional message-status filter.")
    parser.add_argument("--after-seq", type=int, default=0, help="Optional sequence cursor.")
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional maximum number of matching messages; omitted shows all.",
    )
    parser.add_argument("--show-diff", action="store_true", help="Include Git diff text.")
    parser.add_argument("--max-diff-chars", type=int, default=40_000)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--watch-seconds",
        type=float,
        help="Continuously print newly committed messages at this polling interval",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        print(f"No message database found: {db_path}", file=sys.stderr)
        print("Start the agent team first, or pass --db <database path>.", file=sys.stderr)
        return 2
    if args.watch_seconds is not None and args.watch_seconds <= 0:
        raise ValueError("--watch-seconds must be positive")

    queue = SQLiteMessageQueue(db_path)
    registry = RepositoryRegistry(args.registry) if args.registry else None

    if args.message_id:
        print(
            render_message(
                queue.get(args.message_id),
                registry=registry,
                show_diff=args.show_diff,
                max_diff_chars=args.max_diff_chars,
                as_json=args.as_json,
            )
        )
        return 0

    cursor = args.after_seq
    while True:
        messages = queue.list_messages(
            thread_id=args.thread,
            to_role=args.role,
            status=args.status,
            after_seq=cursor,
            limit=args.limit,
        )
        for message in messages:
            print(
                render_message(
                    message,
                    registry=registry,
                    show_diff=args.show_diff,
                    max_diff_chars=args.max_diff_chars,
                    as_json=args.as_json,
                ),
                flush=True,
            )
            cursor = max(cursor, message.seq)

        if args.watch_seconds is None:
            if not messages:
                print(f"No messages in {db_path}.")
            return 0
        time.sleep(args.watch_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
