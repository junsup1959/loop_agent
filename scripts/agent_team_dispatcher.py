from __future__ import annotations

import argparse
import importlib
import json
import socket
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

try:
    from .agent_team_queue import Message, OutboxEvent, SQLiteMessageQueue, WakeHook
except ImportError:
    from agent_team_queue import Message, OutboxEvent, SQLiteMessageQueue, WakeHook


EventHandler = Callable[[dict[str, Any]], None]


class UDPWakeHook:
    """Best-effort loopback wake signal sent only after a queue commit."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port

    def __call__(self, message: Message) -> None:
        payload = json.dumps(
            {"event": "MESSAGE_ENQUEUED", "message_id": message.id},
            separators=(",", ":"),
        ).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.sendto(payload, (self.host, self.port))


class CompositeWakeHook:
    def __init__(self, *hooks: WakeHook) -> None:
        self.hooks = hooks

    def __call__(self, message: Message) -> None:
        errors: list[str] = []
        for hook in self.hooks:
            try:
                hook(message)
            except Exception as exc:
                errors.append(f"{type(hook).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))


def stdout_handler(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def load_handler(import_path: str | None) -> EventHandler:
    if not import_path:
        return stdout_handler
    if ":" not in import_path:
        raise ValueError("handler must be in 'module:function' form")
    module_name, function_name = import_path.split(":", 1)
    module = importlib.import_module(module_name)
    handler = getattr(module, function_name)
    if not callable(handler):
        raise TypeError(f"{import_path} is not callable")
    return handler


class OutboxDispatcher:
    """Wake-driven dispatcher with polling as the durable recovery path."""

    def __init__(
        self,
        queue: SQLiteMessageQueue,
        *,
        handler: EventHandler,
        host: str = "127.0.0.1",
        port: int = 8765,
        poll_interval_seconds: float = 2.0,
        debounce_seconds: float = 0.2,
        batch_size: int = 100,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds cannot be negative")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.queue = queue
        self.handler = handler
        self.host = host
        self.port = port
        self.poll_interval_seconds = poll_interval_seconds
        self.debounce_seconds = debounce_seconds
        self.batch_size = batch_size

    @staticmethod
    def _handler_payload(event: OutboxEvent) -> dict[str, Any]:
        return {
            "outbox": asdict(event),
            "routing": {
                "batch_key": [
                    event.payload.get("work_item_id"),
                    event.payload.get("to_role"),
                ],
                "message_id": event.message_id,
            },
        }

    def drain_once(self) -> int:
        published = 0
        for event in self.queue.pending_outbox(limit=self.batch_size):
            try:
                self.handler(self._handler_payload(event))
            except Exception as exc:
                self.queue.mark_outbox_failed(event.id, error=str(exc))
                print(f"outbox handler failed for {event.id}: {exc}", file=sys.stderr)
            else:
                self.queue.mark_outbox_published(event.id)
                published += 1
        return published

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.settimeout(self.poll_interval_seconds)
            print(
                f"dispatcher listening on udp://{self.host}:{self.port}; "
                f"poll fallback={self.poll_interval_seconds}s",
                flush=True,
            )

            while True:
                awakened = False
                try:
                    server.recvfrom(64 * 1024)
                    awakened = True
                    server.setblocking(False)
                    try:
                        while True:
                            server.recvfrom(64 * 1024)
                    except BlockingIOError:
                        pass
                    finally:
                        server.setblocking(True)
                        server.settimeout(self.poll_interval_seconds)
                except TimeoutError:
                    pass

                if awakened and self.debounce_seconds:
                    time.sleep(self.debounce_seconds)
                self.drain_once()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local SQLite outbox dispatcher with UDP wake-up"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--debounce-seconds", type=float, default=0.2)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--handler",
        help="Optional Python callback in module:function form; receives an event dict",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain the durable outbox once without opening the wake socket",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    queue = SQLiteMessageQueue(args.db)
    dispatcher = OutboxDispatcher(
        queue,
        handler=load_handler(args.handler),
        host=args.host,
        port=args.port,
        poll_interval_seconds=args.poll_seconds,
        debounce_seconds=args.debounce_seconds,
        batch_size=args.batch_size,
    )
    if args.once:
        print(json.dumps({"published": dispatcher.drain_once()}))
        return 0
    try:
        dispatcher.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
