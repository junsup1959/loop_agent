# Local Data Technology Profile

## Discovery

Map each SQLite database, file, configuration store, cache, schema or format version, owner, writer, and recovery path.

## Engineering Focus

- Define transactions, atomicity, locking, connection ownership, busy handling, and concurrent access.
- Check null, default, encoding, locale, path, and serialization assumptions.
- Design forward migration, backup, verification, rollback, and corruption recovery before changing a format.
- Use write-temp, flush, and atomic replace for critical files where applicable.
- Preserve unknown fields when forward compatibility requires it and protect the last recoverable copy.

## Validation

Exercise existing, new, malformed, and previous-version data plus interrupted writes and version transitions. Measure hot queries or file operations before performance changes.
