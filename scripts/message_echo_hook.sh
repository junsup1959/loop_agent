#!/usr/bin/env sh
# Human-only observer for one committed SQLite message.
# This script receives one compact JSON message on stdin. It never writes to
# SQLite, Git, the Context Compiler, or an agent prompt.
set -eu

message="$(cat)"
printf '%s\n' "[agent-team message] ${message}"

if [ -n "${AGENT_TEAM_MESSAGE_LOG:-}" ]; then
    log_directory="$(dirname "$AGENT_TEAM_MESSAGE_LOG")"
    mkdir -p "$log_directory"
    printf '%s\n' "$message" >> "$AGENT_TEAM_MESSAGE_LOG"
fi
