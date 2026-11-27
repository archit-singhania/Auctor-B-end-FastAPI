"""Verify an Auctor backup archive before restoring it into an isolated environment."""

import argparse
import hashlib
import json
import ntpath
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import BadZipFile, ZipFile


def invalid_windows_component(value):
    if any(character in '<>:"|?*' or ord(character) < 32 for character in value):
        return True
    if hasattr(ntpath, 'isreserved'):
        return ntpath.isreserved(value)
    return value.endswith((' ', '.')) or PureWindowsPath(value).is_reserved()


def require_current_backup(manifest, max_age_hours):
    try:
        created = datetime.fromisoformat(manifest['created_at'])
        if created.utcoffset() is None:
            raise ValueError('Timestamp has no offset.')
    except (KeyError, TypeError, ValueError):
        raise ValueError('Backup manifest has no valid creation timestamp.') from None
    age = datetime.now(timezone.utc) - created
    if age < timedelta(minutes=-5):
        raise ValueError('Backup creation timestamp is in the future.')
    if age > timedelta(hours=max_age_hours):
        raise ValueError(f'Backup exceeds the {max_age_hours}-hour freshness limit.')


def verify_archive(path, destination=None, *, max_age_hours=None):
    with ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or names.count('manifest.json') != 1:
            raise ValueError('Archive contains duplicate entries or has no manifest.')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or manifest.get('format_version') != 1:
            raise ValueError('Unsupported backup manifest format.')
        if max_age_hours is not None:
            require_current_backup(manifest, max_age_hours)
        entries = manifest.get('entries')
        if not isinstance(entries, dict) or 'database.dump' not in entries:
            raise ValueError('Manifest has no database dump.')
        if set(names) != {*entries, 'manifest.json'}:
            raise ValueError('Archive members do not match the manifest.')
        for name, expected in entries.items():
            parts = PurePosixPath(name).parts
            if name != 'database.dump' and (
                len(parts) < 2 or parts[0] != 'assets' or '..' in parts
                or name != '/'.join(parts) or '\\' in name or ':' in name
                or any(invalid_windows_component(part) for part in parts)
            ):
                raise ValueError('Manifest contains an invalid asset path.')
            digest = hashlib.sha256()
            size = 0
            output = nullcontext()
            if destination is not None:
                target = destination.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                output = target.open('xb')
            with output as extracted, archive.open(name) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    if extracted is not None:
                        extracted.write(chunk)
            if expected != {'sha256': digest.hexdigest(), 'bytes': size}:
                raise ValueError(f'Backup entry failed verification: {name}')
        return len(entries)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, help='Backup ZIP, or the backup directory with --latest.')
    parser.add_argument('--latest', action='store_true', help='Verify the newest named Auctor backup in the directory.')
    parser.add_argument('--max-age-hours', type=int, choices=range(1, 8761), metavar='HOURS',
                        help='Require a backup created within this many hours (1–8760).')
    args = parser.parse_args()
    try:
        archive = args.archive
        if args.latest:
            from backup import NAME, is_link

            if not archive.is_dir() or is_link(archive):
                raise ValueError('Select a real backup directory with --latest.')
            candidates = [entry for entry in archive.iterdir() if NAME.fullmatch(entry.name)]
            if not candidates:
                raise ValueError('No named Auctor backup archive found.')
            archive = max(candidates, key=lambda entry: entry.name)
            if is_link(archive) or not archive.is_file():
                raise ValueError('The newest backup is not a regular archive.')
        verified = verify_archive(archive, max_age_hours=args.max_age_hours)
    except (BadZipFile, OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    print(f'Verified {verified} backup entries in {archive.name}.')
