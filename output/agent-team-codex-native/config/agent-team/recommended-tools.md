# Recommended Tool Guidance

This document is explicit advisory context for every seat-bound activation. It is not an `AGENTS.md` file and does not make any tool a task, approval, or startup prerequisite.

## Serena

Prefer Serena for semantic source exploration, symbol discovery, reference tracing, focused source excerpts, project onboarding, and slow-changing project-memory summaries. Use only the symbols, paths, and memory references relevant to the current artifact.

If Serena is unavailable, use bounded Git inspection, repository-local search, and verified file reads. Report a missing semantic result only when that exact result is necessary to continue; do not fail a task solely because Serena is unavailable.

## Sequential Thinking

Prefer Sequential Thinking when a plan needs non-trivial work decomposition, dependency reasoning, alternative comparison, or a revision to the Task DAG or Plan IR.

If Sequential Thinking is unavailable, create the same explicit plan and dependency evidence in the task artifact. The quality gate evaluates the plan and evidence, not the tool used to create them.

## Large-Source Research

Prefer the selected `research-loop` workflow for a large web or file corpus. Keep raw material in local artifacts, exchange only artifact references through SQLite, and use the source ledger and claim/evidence records for synthesis. The 10 percent summary ratio is an advisory compression target; never discard or truncate evidence solely to meet it.

## General Rule

Recommended tools improve evidence quality and context efficiency. They never grant authority, write scope, external access, approval rights, or permission to expand context implicitly.
