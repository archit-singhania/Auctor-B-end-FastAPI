# Assessment quality and question governance

Implementation record, November 17, 2026.

## Manual acceptance

1. Sign in as a platform evidence reviewer, then open Evidence → Assessment quality. Organization reviewer membership alone does not grant this permission.
2. Choose an assessment and load its quality report. Fewer than 20 eligible completions suppress item and timing statistics. Counts remain visible. Cohorts below 100 are labelled unstable.
3. Inspect observed correct fractions, completion rates and item-rest associations in the text report. Timing is whole-attempt duration only. A null correlation means there is insufficient variation; it is not zero correlation.
4. Edit the JSON question definitions while preserving IDs 0–4, four options and answer indices 0–3. Enter a review rationale, inspect the full publication preview and confirm.
5. Start an attempt before publishing an edit. Complete it after publication. It uses its saved original prompts and scoring keys. New attempts use the newly published version. Replays return the original stored grade.
6. Set a question's retired field to true and publish. New attempts are blocked with a clear message until a complete five-question version is restored. Existing active attempts can finish using their own snapshot.

## Implementation

Schema version 16 adds append-only published assessment versions and a private question snapshot on new attempts. Delivery strips scoring keys. Grading uses the stored snapshot and retains the existing pass threshold and developer-score formula. Only authorized platform reviewers can inspect answer keys or publish a reviewed version. Publication requires expected-version matching, rationale and explicit confirmation.

Quality-v1 separates cohorts by question version. Eligible completed attempts have on-time, consistent server grades and nonnegative duration. Expired and completed attempts form the closed-attempt completion denominator. Item-rest correlation excludes the item itself from the remaining score. Integrity signals are reported separately and do not automatically remove candidates or alter grades. Reports expose formula version, observation time and an input hash. Legacy attempts without a stored question snapshot are counted separately and excluded rather than assigned an invented version.

## Validation and remaining checks

26 backend integration/unit checks passed in an isolated PostgreSQL schema. They include reproducible 20-attempt fixtures, 19-attempt suppression, revision-bound grading, reviewer permissions, stale publication rejection, retirement and replay preservation. A focused numerical follow-up passed after adding observation timestamps and negative-duration exclusion.

Full browser/reviewer acceptance, screen readers, native packages, older-data migration rehearsal and high-volume performance checks remain open. Reports currently calculate on demand and reject cohorts above 10,000 records; durable asynchronous analytics/export jobs and saved report history are pending. UI loads the current question version; older-version reports are available via the API version parameter. Per-question response timing was never collected and is not inferred. JSON editing is a functional reviewer interface; a dedicated question form remains a usability refinement. Retirement pauses the whole fixed-size challenge rather than silently shortening it. Original code-defined version 1 remains the legacy grading fallback.

All 51 Flutter tests passed, including small-cohort suppression and explicit-publication confirmation at phone/desktop widths. The new reviewer component passed targeted analysis without issues. Production JavaScript web build passed. Existing Wasm/plugin incompatibility and Cupertino font warnings remain.
