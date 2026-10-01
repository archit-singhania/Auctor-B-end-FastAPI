from pathlib import Path

import pytest

from app.config import Settings


def production(tmp_path, **changes):
    config = dict(
        _env_file=None, app_env='production',
        database_url='postgresql://test:fixture@localhost/auctor_test',
        public_api_url='https://api.example.org', web_url='https://web.example.org',
        allowed_origins='https://web.example.org',
        storage_volume_path=str(tmp_path), storage_path=str(tmp_path / 'private'),
        github_client_id='', github_client_secret='',
    )
    config.update(changes)
    return Settings(**config)


def test_production_refuses_development_urls_and_missing_database(tmp_path):
    for changes in (
        {'database_url': None}, {'web_url': 'http://localhost:8041'},
        {'allowed_origins': '*'}, {'allowed_origins': 'https://other.example.org'},
        {'public_api_url': 'https://user:private@api.example.org'},
        {'public_api_url': 'https://127.0.0.1'},
    ):
        with pytest.raises(RuntimeError):
            production(tmp_path, **changes).validate_deployment()


def test_production_refuses_relative_missing_or_outside_private_volume(tmp_path):
    for changes in (
        {'storage_volume_path': ''}, {'storage_path': '_data'},
        {'storage_volume_path': str(tmp_path / 'not-mounted')},
        {'storage_path': str(tmp_path.parent / 'outside-private-volume')},
    ):
        with pytest.raises(RuntimeError):
            production(tmp_path, railway_volume_mount_path='', **changes).validate_deployment()


def test_production_storage_is_writable_and_callback_matches_api(tmp_path):
    config = production(tmp_path)
    config.prepare_storage()
    assert Path(config.storage_path).is_dir()
    assert list(Path(config.storage_path).iterdir()) == []
    assert config.storage_mode == 'private-volume'
    for changes in (
        {'github_client_id': 'fixture-app'},
        {'github_client_id': 'fixture-app', 'github_client_secret': 'fixture-secret',
         'github_redirect_uri': 'https://wrong.example.org/api/github/callback'},
    ):
        with pytest.raises(RuntimeError):
            production(tmp_path, **changes).validate_deployment()
    production(tmp_path, github_client_id='fixture-app', github_client_secret='fixture-secret',
               github_redirect_uri='https://api.example.org/api/github/callback').validate_deployment()


def test_development_keeps_local_preview_and_test_storage(tmp_path):
    config = Settings(_env_file=None, app_env='development', storage_path=str(tmp_path / 'local'))
    config.prepare_storage()
    assert config.storage_mode == 'local-private'
    assert Path(config.storage_path).is_dir()
