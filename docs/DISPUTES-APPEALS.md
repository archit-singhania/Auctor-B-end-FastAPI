# Disputes and appeals

Implementation record, November 17, 2026.

## Manual acceptance

1. Open Evidence → Disputes and appeals → Load disputes. Expand Open a dispute, choose owned evidence, enter a reason and requested correction, and optionally select supporting evidence. Submitting preserves source/support snapshots and leaves verification unchanged.
2. Sign in as an authorized reviewer. Inspect the dispute and original/current source. Independent reviewers exclude the candidate and reviewers with prior decisions or lifecycle actions on the disputed source. If none is available, the dispute remains unassigned.
3. Record reassignment to another eligible reviewer with an explanatory note. Only the assigned independent reviewer can advance review or decide.
4. Start under review, request information, then respond as the candidate. Each transition increments the revision and records actor, assignment, note and time.
5. Inspect the current source, choose a corrected status and confirm correction. Expect a new review decision, updated eligibility, revoked earlier receipts and recalculated score. The original decision and dispute submission remain inspectable. A source change after inspection blocks correction until refreshed.
6. Alternatively uphold the original decision, or withdraw as the candidate. Closed disputes reject further transitions.

## Implementation and validation

Additive schema version 14 stores disputes and append-only application audit entries. Each write checks expected revision and permitted state transition. Corrections lock the current evidence record, compare its hash, append review/lifecycle history, revoke earlier receipts and recalculate eligibility in one transaction. Existing review dates remain in force; an expired date remains stale even after a verified correction. Sources without a proof URL/file cannot be verified through an appeal.

23 backend integration/unit checks passed in an isolated PostgreSQL schema. The appeal journey verified private ownership, duplicate-open rejection, prior-reviewer conflict rejection, assignment, information exchange, stale-source rejection, corrected eligibility and preservation of the original decision.

## Remaining acceptance

Full two-browser reviewer/candidate acceptance, screen readers, restart/migration rehearsal, native packaging and performance checks remain open. Supporting material is snapshotted at submission; later information responses currently accept text only. Source title/detail edits must use existing maintained workflows; appeal correction currently changes verification status and eligibility. Conflicts are detected from recorded decisions and ownership, not external relationships. Authorized platform reviewers can inspect the dispute queue; organization-scoped reviewer access remains unstarted. No independent reviewer means the dispute waits. Unsaved form recovery across process restart is not implemented.

All 47 Flutter regression tests passed, including candidate-only appeal controls at 390 and 1440 pixels. Analysis reported only the existing two informational brace-style findings in presentation_test.dart.

Production JavaScript web build passed. Existing secure-storage Wasm incompatibility and Cupertino font warnings remain; Wasm is not a verified target.
