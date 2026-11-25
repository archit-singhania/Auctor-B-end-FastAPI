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

`BACKUP_RETENTION_DAYS` (default 30) sets the expiration window for archives created by the operator backup command. Configure third-party backup expiration separately. A restored organization ownership conflict can block purge while the account stays disabled; operators must resolve ownership and retry.

Run the backup command from a maintenance job after stopping all application and worker writes. Use a dedicated, access-restricted backup directory outside the private asset directory, and set `PGDATABASE`, `PGHOST`, `PGPORT` and `PGUSER`. Configure credentials through PostgreSQL's password file. The archive contains private data and needs encrypted storage.

```sh
python scripts/backup.py --writes-paused --storage /private/assets \
  --deletion-ledger /private/deletion-ledger --output /restricted/backups
```

The command publishes an archive only after capturing the custom PostgreSQL dump, assets and a SHA-256 manifest. It prunes expired archives from the dedicated destination after a successful backup. Preserve the latest deletion ledger and signing keys separately. Before resuming service from a backup, verify the manifest and restore into an isolated database and asset root with the latest ledger. Resume writes only after verifying account and share revocations.

Workers recover interrupted jobs, serialize archive creation and deletion, and retain status/count receipts without deleted content. Migration backfills use a transaction-local internal write bypass; ordinary requests cannot write to disabled accounts. New uploads use account-scoped directories; tracked legacy asset references are included in cleanup. Unattributable pre-migration orphan files still require operator inventory.

## Scope and limits

Exports include allowlisted owned records, revisions, consent history and referenced source files. Credentials, signing keys, grading keys and private organization notes are excluded. Archive serialization and assets are bounded to 256 MiB, with a 10 MiB per-source-file limit. Unsafe, missing or changed files fail the export rather than produce misleading success. Expired archive cleanup does not delete original account records.

The opt-in `tests/test_backup_recovery.py` rehearsal passed with PostgreSQL 18. It runs the operator backup command, checks entry hashes and expired-archive cleanup, restores into a second database, then starts the application with the latest separately stored deletion ledger. The restored session, sign-in and share stay revoked, the replay purges the restored account and asset, and an unrelated account retains access.

To repeat, set `AUCTOR_TEST_LOCAL=1` and `AUCTOR_TEST_PG_BIN` to the directory containing `pg_dump` and `pg_restore`, then run `python -m pytest tests/test_backup_recovery.py`. The loopback test database role needs permission to create disposable databases. The test uses synthetic accounts. Production backup scheduling, retention, physical-device, two-browser and high-volume checks still require release verification.

## Recorded checks

November 17: 30 backend regression checks and four production-configuration checks passed. All 51 existing Flutter tests and two new responsive deletion fixtures passed in separate runs. JavaScript release web build passed. Analyzer has two existing informational test-style findings; build retains known Wasm secure-storage incompatibility and Cupertino-font warnings. Six repositories showed no log, DLL, PAK, EXE or .gitignore changes.
