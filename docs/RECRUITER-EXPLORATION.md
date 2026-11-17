# Accessible recruiter exploration

Implementation record, November 16, 2026.

## Manual acceptance

1. Open Discover. Tab to the search field, enter a name or declared skill and press Enter. Use the minimum-score field and saved-candidate filter as needed.
2. Choose Compare on up to three profiles. The private comparison tray persists independently of the search results. Search for a different candidate or reload the application, search again and inspect the restored tray.
3. Choose Compact or Comfortable reading. Reload/search and expect the same saved density.
4. Choose Refresh and compare selected profiles. Read declared skills, candidate narrative, assessed badges, current reviewed evidence and textual score contributions separately. Missing evidence is described as unknown. Evidence buttons open the currently authorized public profile.
5. Withdraw discovery on a selected candidate account. Refresh comparison. The candidate payload is withheld and an unavailable count appears. Remove all unavailable profiles to clean the tray.
6. Sign in as a different recruiter. The first account's comparison selection and preferences are not exposed.

## Implementation

Schema version 12 adds owned, revision-bound density and selected-candidate preferences. The server permits three distinct currently discoverable candidates. It rejects stale writes and private selections. Reads recheck discovery consent before assembling comparison payloads. This is a personal workspace and grants no organization access. Organization workspaces remain a separate unstarted addition.

The interface uses native focusable controls, explicit candidate names in actions, section headings, a live tray count and text equivalents for score contributions. A vertical reading order replaces the former horizontally scrolling comparison dialog. Save/unsave, minimum-score and saved-only search controls are retained.

## Acceptance and remaining checks

21 backend integration/unit checks passed, covering saved density/tray state, ownership, invalid duplicate selections, stale writes and discovery withdrawal. Analysis reported only the two pre-existing informational brace-style findings in presentation_test.dart.

Full pointer-free browser navigation, screen-reader output, large-text comparison, native packaging and performance acceptance remain open. Search and comparison data refresh on explicit actions; an already displayed response is not remotely erased when consent changes. Opening evidence rechecks authorization. The comparison shows developer-score components and evidence, not a newly calculated role-fit score. Organization-scoped comparisons await organization-workspace implementation.

All 43 Flutter tests passed, including phone/desktop keyboard-submitted search and tray retention across search results. No generated-binary, log or .gitignore changes appeared across the six repositories.

Production JavaScript web build passed. The existing secure-storage Wasm incompatibility and Cupertino font warning remain. Wasm is not a verified target.
