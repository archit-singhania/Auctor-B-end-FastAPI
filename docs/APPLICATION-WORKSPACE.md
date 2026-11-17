# Private application workspace

Implemented November 16, 2026. Open Profile, then Application workspace.

## Manual acceptance

1. Load applications, then create one. Enter employer, role, optional application date and private notes.
2. Select owned evidence and an existing role-fit run. Save and check the revision label. Refresh and reopen to inspect the saved record.
3. Change stage to applied, interview, offer or closed and save. Switch between list and board views.
4. Add an interview using an ISO timestamp with explicit offset, such as 2026-12-01T10:00:00+05:30. Export saved interviews to a local ICS file. Expect 04:30 UTC in the calendar payload. No guest invitations are generated.
5. Save, then choose Freeze saved material and confirm. Inspect the stored CV, current reviewed requirements, selected role-fit result and evidence metadata. Pending evidence remains pending. The snapshot has a SHA-256 digest and is private.
6. Edit the original CV or application. Inspect the old snapshot from the history button. It stays unchanged. A new application revision supports a new snapshot.
7. Navigate away while editing and return. The in-session draft remains. Close draft explicitly to discard it. Sign-out clears private drafts.

## Implementation and validation

Additive schema version 9 stores application records and immutable per-revision snapshots. Expected revisions prevent stale updates. Identical retries return the existing saved revision. Selected evidence and role-fit results require ownership. Calendar files use UTC times, escaped text and UTF-8 line folding.

18 backend integration/unit checks passed in an isolated PostgreSQL schema. 35 existing Flutter tests and two new application widget tests passed. New widget checks cover 390 and 1440 pixel widths, saving and draft retention after remount. Analysis reported only the two existing informational brace-style findings in presentation_test.dart.

## Remaining acceptance and limitations

Full browser, calendar-client import, native packages, screen-reader and performance checks remain open. Board/list preference is currently session-only. Unsaved drafts survive in-app navigation, not process termination. Interview removal is not exposed yet. There is no automatic stage transition or application delivery. Snapshots capture structured records and historical evidence status, not private source-file contents or proof of sending. Current reviewed requirements and the selected historical role-fit result are separately preserved and may represent different revisions.

Production JavaScript web build passed. The optional Wasm compatibility probe failed on the existing secure-storage plugin, and the build reported the existing Cupertino font warning. Wasm is not a verified target.
