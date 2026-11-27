"""Verify and restore an Auctor archive into a disposable local recovery database."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess

from verify_backup import verify_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--pg-restore', default=shutil.which('pg_restore'))
    args = parser.parse_args()
    if not re.fullmatch(r'auctor_restore_[a-z0-9_]{1,46}', args.database):
        parser.error('Use a new disposable database named auctor_restore_<identifier>.')
    if not args.pg_restore:
        parser.error('Provide pg_restore or add it to PATH.')
    host = os.getenv('PGHOST', '127.0.0.1')
    if host not in {'localhost', '127.0.0.1', '::1'} or os.getenv('PGHOSTADDR') or os.getenv('PGSERVICE'):
        parser.error('Restore requires a direct loopback PostgreSQL connection.')
    companion = Path(args.pg_restore).with_name('psql.exe' if os.name == 'nt' else 'psql')
    psql = str(companion) if companion.is_file() else shutil.which('psql')
    if not psql:
        parser.error('Install psql alongside pg_restore or add it to PATH.')
    connection = ['--host', host, '--dbname', args.database, '--no-password']
    nonempty = subprocess.run([
        psql, *connection, '--no-psqlrc', '--tuples-only', '--no-align',
        '--set', 'ON_ERROR_STOP=1', '--command', """
        SELECT
            EXISTS (SELECT FROM pg_namespace WHERE nspname NOT IN
                ('public', 'pg_catalog', 'information_schema') AND left(nspname, 3) <> 'pg_')
            OR EXISTS (SELECT FROM pg_class WHERE relnamespace = 'public'::regnamespace)
            OR EXISTS (SELECT FROM pg_proc WHERE pronamespace = 'public'::regnamespace)
            OR EXISTS (SELECT FROM pg_type WHERE typnamespace = 'public'::regnamespace)
            OR EXISTS (SELECT FROM pg_extension WHERE extname <> 'plpgsql');
        """,
    ], check=True, capture_output=True, text=True).stdout.strip()
    if nonempty != 'f':
        parser.error('Restore destination must be an empty database.')
    args.destination.mkdir(mode=0o700)
    count = verify_archive(args.archive, args.destination)
    (args.destination / 'assets').mkdir(exist_ok=True)
    subprocess.run([
        args.pg_restore, *connection,
        '--single-transaction', '--exit-on-error', '--no-owner', '--no-privileges',
        str(args.destination / 'database.dump'),
    ], check=True)
    print(f'Restored {count} verified entries. Start the isolated API with the latest deletion ledger before allowing access.')


if __name__ == '__main__':
    main()
