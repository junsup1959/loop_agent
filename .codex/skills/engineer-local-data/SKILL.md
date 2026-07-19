---
name: engineer-local-data
description: Implement and review local persistence using SQLite, files, configuration stores, caches, and versioned data formats with transaction, migration, corruption, recovery, performance, and backward-compatibility controls. Use when an offline application owns data on the local machine.
---

# Engineer Local Data

Preserve local data integrity and previous-version compatibility across normal use, crashes, upgrades, and recovery.

## Procedure

1. Map every local store, owning component, schema or format version, and write path.
2. Define atomicity, transaction, locking, and concurrent access behavior.
3. Check null, default, encoding, path, locale, and serialization assumptions.
4. Design forward migration, rollback or recovery, and backup behavior before changing a format.
5. Validate existing data, new data, malformed data, interrupted writes, and version transitions.
6. Measure hot queries or file operations before making performance claims.

## SQLite Rules

- Use explicit transactions for multi-step invariants.
- Check journal mode, busy handling, foreign keys, and connection ownership.
- Make migrations ordered, idempotent where practical, and recoverable.
- Base indexes and query changes on observed access patterns.

## File and Configuration Rules

- Prefer write-temp, flush, and atomic replace for critical files.
- Preserve unknown fields when forward compatibility requires it.
- Never overwrite the only recoverable copy before validation succeeds.
- Keep secrets out of plain configuration unless the task explicitly defines a secure storage model.

## Authority Boundary

This skill does not grant schema approval, access to user data, destructive migration permission, file ownership, model selection, or tool permissions.
