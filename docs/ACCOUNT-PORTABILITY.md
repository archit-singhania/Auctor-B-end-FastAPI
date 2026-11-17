# Account portability and retention

Implementation review: November 17, 2026. Source implementation; full browser/native release acceptance remains pending.

## Manual acceptance

1. Sign in to a disposable account and open Profile → Account portability. Queue an export and refresh until ready. Download and unzip it: inspect records, original referenced files, manifest and SHA-256 checksums. Another account must not download it. Failed exports expose retry; changing retention affects future archives only (1–30 days).
2. Open the account-deletion panel and request a fresh inventory. Organization owners must first transfer ownership to an active member in Organizations. Existing members cannot transfer ownership themselves.
3. Review the inventory, enter the current password and `DELETE MY ACCOUNT`, then confirm. Access and sharing are revoked immediately. Save the private deletion receipt before leaving the status page; refresh does not preserve an unsaved receipt.
4. Check the public deletion-status page using the receipt. Cleanup reports queued/running/completed or failed. Failed cleanup remains inaccessible and can be retried. Test this only with disposable accounts.
5. Verify that another account and its files remain intact. Historical reviews belonging to others may retain anonymized decisions; account-owned data and attributable assets are purged.

## Recovery and deployment

Set `DELETION_LEDGER_PATH` to an absolute directory inside the configured persistent volume in production. Preserve this ledger separately from database/asset backups, including a protected external copy. Entries contain an opaque account key and request time, not deleted content. Startup replays tombstones before serving requests, preventing restored accounts and consent from reactivating. Malformed ledger entries fail startup closed. Never restore an older ledger over a newer one.

`BACKUP_RETENTION_DAYS` (default 30) documents the operator's backup policy; it does not delete third-party backups. Configure and verify backup expiration separately. Active-store deletion is not immediate erasure from retained backups. A restored organization ownership conflict can block purge while the account stays disabled; operators must resolve ownership and retry.

Workers recover interrupted jobs, serialize archive creation and deletion, and retain status/count receipts without deleted content. Migration backfills use a transaction-local internal write bypass; ordinary requests cannot write to disabled accounts. New uploads use account-scoped directories; tracked legacy asset references are included in cleanup. Unattributable pre-migration orphan files still require operator inventory.

## Scope and limits

Exports include allowlisted owned records, revisions, consent history and referenced source files. Credentials, signing keys, grading keys and private organization notes are excluded. Archive serialization and assets are bounded to 256 MiB, with a 10 MiB per-source-file limit. Unsafe, missing or changed files fail the export rather than produce misleading success. Expired archive cleanup does not delete original account records.

Full physical-device, two-browser, complete database-backup restoration, high-volume and performance checks remain release gates. Synthetic restored-account tests do not certify an operator's backup system.

## Recorded checks

November 17: 30 backend regression checks and four production-configuration checks passed. All 51 existing Flutter tests and two new responsive deletion fixtures passed in separate runs. JavaScript release web build passed. Analyzer has two existing informational test-style findings; build retains known Wasm secure-storage incompatibility and Cupertino-font warnings. Six repositories showed no log, DLL, PAK, EXE or .gitignore changes.
