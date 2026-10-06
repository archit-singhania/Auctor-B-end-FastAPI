# Auctor API presentation contract · October 6, 2026

The companion Flutter workspace now uses short evidence, route, sheet and confirmed-result transitions and three curated personal appearance palettes. The API remains the source of assessment results, score deltas, proof status and durable reviewer decisions. Client animation never adds progress, points or verification.

Personal palettes use the existing bounded `preferences` JSON field and profile PATCH/readback contract. No endpoint, schema, score weight or access-rule change is needed. Unknown client palette values fall back to the canonical edition in Flutter. The API's PDF report and public SVG badge retain the existing ivory/jade/graphite/champagne identity: an exported/shared report carries Auctor's brand independently of a viewer's personal accent choice. Their current serif display/clear evidence typography and contact redaction remain appropriate, so export source is unchanged.

The current isolated local PostgreSQL/API check passed **18 tests in 16.94 seconds**, including owned server grading/replay, independent review/audit, preference persistence, PDF/SVG endpoints, sharing privacy and production guards. Tests create their own temporary schema and private-file directory. Actual companion-browser reviewer checks use generated, labelled synthetic accounts and alter only their own evidence.

See the Flutter repository's `docs/PREMIUM-MOTION-2026-10-06.md` and machine/browser receipts for current web/Android fingerprints, screenshots, reduced-motion checks and manual expectations. [This API receipt](premium-motion-2026-10-06.json) fingerprints the unchanged export and preference implementation alongside its current verification result.

To inspect exports manually, run [the local API setup](MANUAL_TESTS.md), export a report from a signed-in synthetic profile and inspect its readable score/evidence/page labels. Make a test profile discoverable before opening its public SVG badge. Public profiles and reports must continue to omit contacts and private proof files. Live provider credentials and public deployment remain separate manual gates.
