# Evidence collection assistant

Implemented November 16, 2026. This feature derives private checklists from saved CV claims and career milestones. Source links persist in an additive collection_links table. Claim content hashes prevent changed claims from silently inheriting earlier links. Current reviewer state determines source eligibility on each refresh. Linking a source never grants verification or changes a developer score.

## Manual acceptance

1. Sign in and save a Python skill or a project in the CV.
2. Open Evidence and choose Load collection checklists. Expect claim-specific guidance and a missing-source status.
3. Submit supporting material using the existing submission dialog. It remains pending review.
4. Expand the claim, select an owned source and attach it. Refresh and reload the application. The saved link remains, with its actual review status.
5. Have an authorized reviewer review the source. Refresh to see eligibility. Stale/revoked sources must be identified accordingly.
6. Edit the original CV claim. Its changed content starts a new checklist identity. Another account cannot see or attach the original private evidence.

## Validation

All 17 backend integration and collection unit checks passed in an isolated local PostgreSQL schema. Includes idempotent attachment, ownership rejection, stale claim rejection, missing/pending/stale/revoked evidence and incompatible source kinds.

Full browser, native/device, screen-reader and packaged acceptance remain outstanding. Candidate-selected source links express organization and reviewer scope, not independent validation of every connected claim. Checklist sources are current CV and career narrative claims. Removing candidate-created collection links is not yet exposed.

Flutter verification passed all 33 existing widget tests and two new collection tests at 390 and 1440 pixel widths. Analysis reported only two existing informational brace-style findings in presentation_test.dart. Initial dependency setup could not create Windows plugin symlinks; tests succeeded with resolved packages and --no-pub. A fresh package build remains outstanding.
