"""Create an offline PostgreSQL and private-asset backup for operator recovery."""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile


NAME = re.compile(r'auctor-backup-(\d{8}T\d{6}Z)-[a-f0-9]{32}\.zip')


def is_link(path):
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def raise_walk_error(error):
    raise error


def add_file(archive, source, name):
    with source.open('rb') as stream, archive.open(name, 'w', force_zip64=True) as output:
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
            size += len(chunk)
    return {'sha256': digest.hexdigest(), 'bytes': size}


def backup(output, storage, ledger, pg_dump, retention_days):
    if not storage.is_dir() or is_link(storage):
        raise ValueError('Use an existing private storage root.')
    output, storage, ledger = output.resolve(), storage.resolve(), ledger.resolve()
    if output.is_relative_to(storage) or storage.is_relative_to(output):
        raise ValueError('Backup destination must be separate from private storage.')
    if output.is_relative_to(ledger) or ledger.is_relative_to(output):
        raise ValueError('Keep the live deletion ledger outside the backup destination.')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    prefix = f'auctor-backup-{now:%Y%m%dT%H%M%SZ}-{uuid4().hex}'
    staged = output / f'.{prefix}.partial'
    final = output / f'{prefix}.zip'
    try:
        with tempfile.TemporaryDirectory(dir=output, prefix='.auctor-dump-') as temporary:
            dump = Path(temporary) / 'database.dump'
            subprocess.run([pg_dump, '--no-password', '--format=custom', '--file', str(dump)],
                           check=True)
            with staged.open('xb') as stream, ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
                entries = {'database.dump': add_file(archive, dump, 'database.dump')}
                for current, folders, files in os.walk(storage, onerror=raise_walk_error):
                    directory = Path(current)
                    folders[:] = sorted(
                        folder for folder in folders
                        if not is_link(directory / folder)
                        and not (directory / folder).resolve().is_relative_to(ledger)
                    )
                    for filename in sorted(files):
                        asset = directory / filename
                        if is_link(asset) or not asset.is_file() or asset.resolve().is_relative_to(ledger):
                            continue
                        name = 'assets/' + asset.relative_to(storage).as_posix()
                        entries[name] = add_file(archive, asset, name)
                archive.writestr('manifest.json', json.dumps({
                    'format_version': 1, 'created_at': now.isoformat(), 'entries': entries,
                }, indent=2))
            staged.replace(final)
    finally:
        staged.unlink(missing_ok=True)

    cutoff = now - timedelta(days=retention_days)
    for candidate in output.iterdir():
        match = NAME.fullmatch(candidate.name)
        if not match or is_link(candidate) or not candidate.is_file():
            continue
        try:
            created = datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created < cutoff:
            candidate.unlink()
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--deletion-ledger', type=Path, required=True)
    parser.add_argument('--pg-dump', default=shutil.which('pg_dump'))
    parser.add_argument('--retention-days', type=int, default=os.getenv('BACKUP_RETENTION_DAYS', '30'))
    parser.add_argument('--writes-paused', action='store_true')
    args = parser.parse_args()
    if not args.writes_paused:
        parser.error('Stop application and worker writes before capturing database and files.')
    if not args.pg_dump or not 1 <= args.retention_days <= 3650:
        parser.error('Provide pg_dump and a retention period of 1–3650 days.')
    if not os.getenv('PGDATABASE'):
        parser.error('Set PGDATABASE and PostgreSQL connection variables for the backup source.')
    print(backup(args.output, args.storage, args.deletion_ledger, args.pg_dump, args.retention_days))


if __name__ == '__main__':
    main()
