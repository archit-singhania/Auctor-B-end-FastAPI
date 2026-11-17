# Portfolio case-study builder

Implementation record, November 16, 2026.

Open Profile → Portfolio case study → Load case study. Expand Problem, Contribution, Approach, Outcome and Reflection. Enter your narrative and select supporting evidence for each section. Save, then preview the saved publication. Pending evidence is excluded from the public evidence list. Narrative is explicitly candidate-written.

Review all displayed text before confirming publication. The public link expires after seven days and supports HTML and PDF exports. Edit the private draft and save again. The earlier publication remains unchanged. Revoke a publication to deny subsequent HTML/PDF access. Previously downloaded files cannot be recalled.

## Storage and privacy

Schema version 10 adds owned draft revisions and independent publication snapshots. Saving requires the expected draft revision. Publishing binds to a digest of the reviewed projection. Evidence links preserve the source update timestamp and a metadata snapshot hash. Public responses omit source URLs, private details and source files. A changed, stale, deleted or revoked source is withheld from historical publications. Private candidate narrative is never automatically redacted; the public preview explicitly requires author review.

## Acceptance

19 backend integration/unit checks passed, including ownership, stale previews, unchanged published narrative after draft edits, HTML escaping, PDF output, private-detail exclusion, evidence revocation and full share revocation. Flutter analyzer reported only the two existing informational brace-style findings in presentation_test.dart.

## Remaining release checks

Browser, native packages, screen-reader, PDF visual inspection and performance acceptance remain open. HTML uses semantic headings and a readable responsive layout; PDF is not claimed to meet tagged-PDF accessibility standards. Current UI supports one active draft with multiple immutable publications. Draft history is stored but restoration is not yet exposed. Unsaved edits survive in-app navigation through the controller and are cleared on sign-out; process-restart draft recovery remains open. PDF generation uses the existing bounded presentation renderer and runs off the event loop, but durable export jobs are not yet implemented.

37 existing Flutter tests and two new phone/desktop preview tests passed. Production JavaScript web build passed. The optional Wasm probe remains incompatible with the existing secure-storage plugin, and the existing Cupertino font warning remains.
