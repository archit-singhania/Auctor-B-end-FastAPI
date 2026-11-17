# Opt-in profile analytics

Implementation record, November 16, 2026.

## Manual acceptance

1. Open Profile → Profile analytics → Load analytics. A new account starts disabled with no recorded interactions.
2. Enable collection and make the profile discoverable using existing profile settings.
3. Open the public profile from another browser session. Reload analytics to inspect the UTC daily profile_view count. Open an eligible evidence source to record evidence_open. Access through a live profile share also records share_activity.
4. Refresh repeatedly within ten seconds. Requests to the same content resource are merged across all visitors. This intentionally undercounts simultaneous visitors and is never presented as unique-person measurement.
5. Preview your own profile while signed in. Your authenticated owner session is excluded. Anonymous owner visits cannot be identified and may count.
6. Disable collection. New visits leave counts unchanged. Reset analytics requires confirmation and deletes the aggregates without changing consent.
7. Revoke a profile share. Further events and evidence opens through that share fail. Make the profile private. Events stop even through otherwise valid private share links.

## Privacy and persistence

Schema version 11 stores owner consent, UTC daily counts and short-burst content keys. The keys represent public content resources, not visitors. No IP addresses, user agents, fingerprint, session token or persistent visitor identifiers enter these tables. Current authenticated identity is checked only to exclude owner previews. Historical data survives restarts; only the account owner can read/reset it.

Public profile load failures do not generate events. Analytics failures do not block profile rendering. Evidence opens recheck current sharing permissions and current evidence eligibility before returning the source URL. Counts describe requested opens, not confirmation that an external site loaded. Bots, direct API calls and missing client requests affect coverage. Case-study and recruiter-presentation traffic is outside this initial profile/share instrumentation.

## Validation and remaining acceptance

20 backend integration/unit checks passed in an isolated PostgreSQL schema, including disabled-by-default behavior, opt-in, private-profile exclusion, owner exclusion, burst merging, evidence eligibility, owner-only reports, reset and share revocation. Analysis reported only two existing informational brace-style findings in presentation_test.dart.

Full browser, multiple-tab identity loading, screen-reader, native package and performance acceptance remain open. The daily report currently returns up to 1098 aggregate rows. Previously recorded data is retained until reset or account deletion; disabling alone preserves it. Ten-second suppression keys are pruned on the next eligible event or reset, and contain no visitor data.

All 41 Flutter tests passed, including phone/desktop consent and reset confirmation checks. No log, .gitignore, DLL, PAK or EXE changes appeared in any of the six repositories.

Production JavaScript web build passed. The existing secure-storage Wasm incompatibility and Cupertino font warning remain; Wasm is not a verified release target.
