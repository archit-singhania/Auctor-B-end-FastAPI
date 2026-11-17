# Evidence lineage

Implementation record, November 16, 2026.

## Manual acceptance

1. Open Evidence → Evidence lineage → Load source history. Choose Trace source.
2. Expand a captured revision. Inspect the original fields and SHA-256 digest. The API also returns the exact canonical text used for hashing, so the digest can be independently reproduced.
3. Review an evidence source and publish a case-study section linked to it. Trace the source again. Expect a published-claim reference with the original source update timestamp, metadata hash and matching captured revision IDs.
4. Change or withdraw the source. Refresh lineage. Earlier snapshots remain unchanged; current eligibility and withdrawal are separate from history. Published content continues to apply its own current-access redaction.
5. Inspect receipt, reviewer-decision, application-snapshot and candidate-link references where available. Binding descriptions distinguish exact snapshots from live candidate links and incomplete older history.
6. Sign in with another account. Private source lineage must return unavailable.

## Implementation

Schema version 13 adds transactionally captured evidence INSERT/UPDATE/DELETE snapshots. A database trigger captures changes made by all maintained write paths. Storage keys are omitted. Each row stores PostgreSQL JSONB canonical text and its SHA-256 digest. Existing evidence receives an explicitly labelled migration baseline; earlier states are not fabricated. The application exposes no history-update operation. Account deletion can purge owned history through the user foreign key.

Owned API queries derive downstream references from already persisted review inputs, signed receipts, publication snapshots, application snapshots and candidate-selected collection links. Historical receipt hashes are matched against captured payloads when possible. Case-study references preserve their original revision and metadata hash. Multiple timestamp matches are reported as ambiguous instead of being labelled exact.

## Validation and limits

22 backend checks passed in an isolated PostgreSQL schema. The lineage journey verified capture, digest reproduction, a published claim's exact revision, ownership isolation, deletion capture and unchanged original history. All 43 existing Flutter tests and two new phone/desktop history-inspector tests passed. Analysis reported only two existing informational brace-style findings in presentation_test.dart.

Browser, screen-reader, native release, high-volume query performance and full older-data migration rehearsal remain open. The current graph is an accessible source/references list. Raw document bytes and extracted CV claims are not yet represented as universal lineage nodes. Old deleted sources cannot be recovered if no historical snapshot exists. Reviewer decision payloads use their existing stored source fields, which may be incomplete. Recruiter presentations and all legacy publication formats still need coordinated lineage coverage. Hashes establish snapshot identity, not the truth of evidence or protection against a database administrator rewriting history.

November 17 follow-up: production JavaScript web build passed, with the existing Wasm/plugin and font warnings. Published-source matching now requires both timestamp and metadata hash. A focused integration rerun passed after normalizing database timestamps to UTC. Receipt matching uses the same timestamp normalization.
