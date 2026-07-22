# Integration Validation Profile

## Entry Conditions

- PL has approved an immutable base OID, ordered candidate OIDs, merge strategy, and decision identifier.
- Every candidate has the required independent TA evidence.

## Validation Standard

- The mechanical controller performs no code judgment or conflict repair.
- Conflicts preserve base, merge bases, ordered candidates, paths, index stages, partial head, commands, logs, and tool versions.
- A clean merge produces a new immutable integration OID.
- QA and Build must independently validate that same OID before PL integration approval.

## Failure Rule

Conflicts or post-merge failures become PL-owned rework. No reviewer or controller edits the integration workspace in place.
