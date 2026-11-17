# Organization workspaces

Implementation record, November 17, 2026.

## Manual acceptance

1. Open Discover → Organization workspace. Create a named workspace. You become its protected owner.
2. Expand Members and invitations. Enter a recipient email, choose reviewer or administrator, and create an invitation. Copy the one-time-displayed token to the intended recipient yourself. The application sends no email.
3. Sign in with the matching email and accept the token. Wrong accounts, expired, accepted and revoked tokens fail. Reviewers cannot manage invitations. Only owners can grant administrator roles.
4. Search discoverable candidates and save a profile to the organization. Add a private review note and select up to three profiles for the shared comparison. Reload and expect saved notes and comparison selection.
5. A second member can see the same organization data; unrelated accounts cannot access its endpoints. Concurrent note/comparison changes reject stale revisions.
6. Turn off candidate discovery. Refresh the organization. Candidate payload and organization notes are withheld and the comparison excludes that candidate. Membership never grants private file access.
7. Remove a member as an owner or permitted administrator. Their next read/write fails and pending invitations addressed to that account are revoked. The owner cannot be removed or demoted.
8. Remove a visible saved profile with confirmation. Its organization note and comparison selection are deleted. This does not delete candidate evidence.

## Implementation

Schema version 15 stores organizations, memberships, hashed seven-day email-bound invitations, organization-owned review notes, revision-bound comparisons and activity history. Membership mutations and scoped writes serialize on the organization row. Candidate consent is rechecked on saved-profile reads and writes. Organization membership roles are distinct from platform evidence-reviewer privileges and do not grant appeal or evidence-verification permissions.

## Validation and remaining work

24 backend integration/unit checks passed in an isolated PostgreSQL schema. A focused removal follow-up also passed. All 49 Flutter regression tests passed, including phone/desktop membership and consent-withdrawal layouts. Analysis reported only the two existing informational brace-style findings in presentation_test.dart.

Two-browser invitation/member-removal journeys, native packaging, screen-reader testing, high-volume behavior and migration/recovery rehearsals remain open. Owner transfer is now exposed to the current owner with explicit confirmation and active-member validation; organization deletion remains pending. Current candidate access uses existing public discovery consent; organization-specific grants to private profiles are not implemented. Organization reviewer notes are not evidence decisions. Refresh is required to remove already displayed data after consent withdrawal. Unavailable saved profiles can be removed through the API with their known IDs; the UI withholds their identifying data. Organization scope is a separate workspace and is not yet selectable in the personal recruiter comparison tray.

Final production JavaScript web rebuild passed after saved-profile removal was added. The focused organization widget rerun passed. Existing secure-storage Wasm incompatibility and Cupertino font warnings remain; Wasm is not a verified target. No generated-binary, log or .gitignore changes appeared across the six repositories.
