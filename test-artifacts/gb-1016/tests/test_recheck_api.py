"""API-level check for sealing/rechecking through FastAPI + SQLite."""
import os
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'api.db'))
    monkeypatch.setenv('REDIS_URL', 'redis://localhost:6399/0')
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('SEED_DEMO', 'false')
    for name in list(sys.modules):
        if name.split('.')[0] in ('src', 'scripts', 'migrations'):
            del sys.modules[name]
    from fastapi.testclient import TestClient
    import src.main as main
    with TestClient(main.app) as test_client:
        yield test_client, main


def login(client, email):
    r = client.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def seed_job(client):
    from scripts.seed_demo import seed
    seed()
    from src.config.database import Session
    from src.models import Job
    from sqlalchemy import select
    with Session() as db:
        return db.scalar(select(Job).where(Job.name.like('基线复核%')))


def test_full_seal_and_recheck_flow(client):
    test_client, _ = client
    job = seed_job(test_client)
    admin = login(test_client, 'admin@demo.local')
    detail = test_client.get(f'/jobs/{job.id}', headers=admin).json()
    assert detail['seal']['algorithm_version']
    assert detail['seal']['expected_days'] == 32
    assert detail['recheck'] is None

    r = test_client.post(f'/jobs/{job.id}/recheck', headers=admin)
    assert r.status_code == 202, r.text
    assert r.json()['recheck']['status'] == 'pending'

    # worker drains the pending review
    from src.worker import rechecks
    rechecks()

    r = test_client.get(f'/jobs/{job.id}/recheck', headers=admin)
    body = r.json()
    assert body['recheck']['verdict'] == 'passed'
    assert body['recheck']['checked_days'] == 32
    assert body['recheck']['differences'] == []
    assert len(body['recheck']['days']) == 32
    assert body['recheck']['algorithm_used'] == body['seal']['algorithm_version']

    # idempotent: a repeated request does not create a second review
    test_client.post(f'/jobs/{job.id}/recheck', headers=admin)
    from src.config.database import Session
    from src.models import Recheck
    from sqlalchemy import func, select
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Recheck)) == 1

    # original conclusion and delivered brief are still available
    assert test_client.get(f'/jobs/{job.id}', headers=admin).json()['result']['summary'] == job.result['summary']
    deliveries = test_client.get('/deliveries', headers=admin).json()
    assert deliveries and test_client.get(f'/deliveries/{deliveries[0]["id"]}/download?format=csv', headers=admin).status_code == 200


def test_recheck_requires_admin(client):
    test_client, _ = client
    job = seed_job(test_client)
    analyst = login(test_client, 'analyst@demo.local')
    viewer = login(test_client, 'viewer@demo.local')
    assert test_client.post(f'/jobs/{job.id}/recheck', headers=analyst).status_code == 403
    assert test_client.post(f'/jobs/{job.id}/recheck', headers=viewer).status_code == 403
    assert test_client.get(f'/jobs/{job.id}/recheck', headers=viewer).status_code == 200
