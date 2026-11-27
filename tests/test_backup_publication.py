"""Verify that a failed backup cannot displace existing recovery archives."""

from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import backup


def test_reject_unverified_archive_before_publication_and_retry(tmp_path, monkeypatch):
    destination = tmp_path / 'backups'
    destination.mkdir()
    expired = destination / f'auctor-backup-20200101T000000Z-{"0" * 32}.zip'
    expired.write_bytes(b'prior backup')
    storage = tmp_path / 'assets'
    storage.mkdir()
    (storage / 'proof.pdf').write_bytes(b'private asset')
    ledger = tmp_path / 'ledger'

    def dump(args, *, check):
        assert check
        Path(args[-1]).write_bytes(b'fixture dump')

    monkeypatch.setattr(backup.subprocess, 'run', dump)
    verify = backup.verify_archive

    def reject(staged):
        with ZipFile(staged, 'a') as archive:
            archive.writestr('unexpected', b'incomplete publication')
        return verify(staged)

    monkeypatch.setattr(backup, 'verify_archive', reject)
    with pytest.raises(ValueError, match='members do not match'):
        backup.backup(destination, storage, ledger, 'pg_dump', 30)
    assert list(destination.iterdir()) == [expired]
    assert expired.read_bytes() == b'prior backup'

    monkeypatch.setattr(backup, 'verify_archive', verify)
    published = backup.backup(destination, storage, ledger, 'pg_dump', 30)
    assert verify(published) == 2
    assert list(destination.iterdir()) == [published]
