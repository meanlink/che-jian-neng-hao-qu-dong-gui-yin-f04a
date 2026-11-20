"""Recheck / recomputation contract tests.

Run with:  python -m pytest test-artifacts/gb-1016/tests -q
"""
import os
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(BACKEND))
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(Path(__file__).with_name('recheck-test.db')))
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('ARTIFACT_DIR', str(Path(__file__).with_name('artifacts')))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.config.database import Base  # noqa: E402
from src.models import Dataset, Job, Recheck, RecheckDay, SealedTrial, User  # noqa: E402
from src.services import algorithms, sealed  # noqa: E402
from src.services.business import analyze, seed_records  # noqa: E402
from src.utils.serialization import checksum  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'test.db'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    session = Session()
    session.add(User(email='admin@test.local', name='负责人', role='admin', password='x:y'))
    session.commit()
    yield session
    session.close()
    engine.dispose()


PARAMS = {'max_features': 4, 'scenario_pct': 10}


def make_job(db, params=None):
    records = seed_records()
    dataset = Dataset(name='批次', source='t.csv', records=records, checksum=checksum(records), created_at=time.time())
    db.add(dataset)
    db.flush()
    result = analyze(records, params or PARAMS)
    job = Job(name='试验', dataset_id=dataset.id, params=params or PARAMS, status='completed', result=result,
              created_by=1, created_at=time.time())
    db.add(job)
    db.commit()
    sealed.seal(db, job, dataset)
    db.commit()
    return job, dataset


def request_and_run(db, job):
    review = sealed.requested(db, job, 1)
    db.commit()
    return sealed.run(db, review.id)


def test_new_trial_seals_sample_params_version_and_daily_holdout(db):
    job, dataset = make_job(db)
    seal = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    assert seal is not None
    assert seal.algorithm_version == algorithms.CURRENT_VERSION
    assert seal.params == job.params
    assert len(seal.sample) == 160
    assert seal.expected_days == 32
    assert all({'day', 'actual', 'predicted'} <= set(row) for row in seal.holdout)


def test_recheck_passes_and_records_every_holdout_day(db):
    job, _ = make_job(db)
    review = request_and_run(db, job)
    assert review.status == 'completed' and review.verdict == 'passed'
    assert review.checked_days == 32
    assert db.scalar(select(func.count()).select_from(RecheckDay).where(RecheckDay.recheck_id == review.id)) == 32


def test_upgrade_keeps_original_algorithm_for_old_trial(db, monkeypatch):
    job, _ = make_job(db)
    calls = []

    def upgraded(records, params):
        calls.append('v2')
        return {**analyze.__globals__['_analyze_v1'](records, params), 'algorithm_version': 'energy-ridge/2.0.0'}

    monkeypatch.setitem(algorithms._VERSIONS, 'energy-ridge/2.0.0', upgraded)
    monkeypatch.setattr(algorithms, 'CURRENT_VERSION', 'energy-ridge/2.0.0')
    assert analyze(seed_records(), PARAMS)['algorithm_version'] == 'energy-ridge/2.0.0'
    calls.clear()
    review = request_and_run(db, job)
    assert calls == []
    assert review.algorithm_original == 'energy-ridge/1.0.0'
    assert review.algorithm_used == 'energy-ridge/1.0.0'
    assert review.verdict == 'passed'


def test_legacy_trial_without_seal_is_unverifiable(db):
    job, _ = make_job(db)
    db.delete(db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id)))
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'unverifiable'
    assert '缺少复算证据' in review.reason


def test_removed_algorithm_version_is_unverifiable(db):
    job, _ = make_job(db)
    seal = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    seal.algorithm_version = 'energy-ridge/0.9.0'
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'unverifiable'
    assert '0.9.0' in review.reason


def test_material_tamper_fails(db):
    job, dataset = make_job(db)
    seal = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    seal.sample[0]['units'] += 1
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'failed'
    assert '材料校验失败' in review.reason


def test_dataset_changed_after_seal_fails(db):
    job, dataset = make_job(db)
    dataset.records = [dict(row) for row in dataset.records]
    dataset.records[0]['kwh'] = dataset.records[0]['kwh'] + 5
    dataset.checksum = checksum(dataset.records)
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'failed'
    assert '数据批次内容' in review.reason


def test_missing_holdout_date_fails(db):
    job, _ = make_job(db)
    seal = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    seal.holdout = seal.holdout[:-1]
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'failed'
    assert '条数' in review.reason or '缺少' in review.reason


def test_tampered_prediction_over_one_thousandth_fails(db):
    job, _ = make_job(db)
    seal = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    seal.holdout[0]['predicted'] += 0.002
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'failed'
    days = list(db.scalars(select(RecheckDay).where(RecheckDay.recheck_id == review.id, RecheckDay.ok == False)))  # noqa: E712
    assert [row.day for row in days] == [seal.holdout[0]['day']]
    assert '差异日期' in review.reason
    detail = sealed.detail(db, db.get(Recheck, review.id))
    assert detail['differences'] == [seal.holdout[0]['day']]


def test_tolerance_is_inclusive_at_exactly_one_thousandth(db):
    job, _ = make_job(db)
    result = analyze(seed_records(), PARAMS)
    seal = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    seal.holdout[0]['predicted'] = result['holdout'][0]['predicted'] - 0.001
    db.commit()
    review = request_and_run(db, job)
    assert review.verdict == 'passed'


def test_interrupted_recheck_resumes_without_duplicate_rows(db):
    job, _ = make_job(db)
    review = sealed.requested(db, job, 1)
    db.commit()
    assert sealed._claim(db, review)
    db.refresh(review)
    # Simulate: first 10 dates were checked before the process died.
    archived = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id)).holdout
    for row in archived[:10]:
        db.add(RecheckDay(recheck_id=review.id, day=row['day'], actual=row['actual'],
                          archived=row['predicted'], replayed=row['predicted'], delta=0.0, ok=True))
    review.checked_days = 10
    review.locked_at = time.time() - 60  # lease expired
    db.commit()
    review = sealed.run(db, review.id)
    assert review.status == 'completed' and review.verdict == 'passed'
    assert review.checked_days == 32
    assert db.scalar(select(func.count()).select_from(RecheckDay).where(RecheckDay.recheck_id == review.id)) == 32


def test_repeated_requests_register_one_review(db):
    job, _ = make_job(db)
    first = sealed.requested(db, job, 1)
    for _ in range(5):
        again = sealed.requested(db, job, 1)
        assert again.id == first.id
    assert db.scalar(select(func.count()).select_from(Recheck).where(Recheck.job_id == job.id)) == 1
    sealed.run(db, first.id)
    sealed.run(db, first.id)
    assert db.scalar(select(func.count()).select_from(Recheck).where(Recheck.job_id == job.id)) == 1
    assert db.get(Recheck, first.id).verdict == 'passed'


def test_concurrent_claim_only_one_runner_wins(db):
    job, _ = make_job(db)
    review = sealed.requested(db, job, 1)
    db.commit()
    assert sealed._claim(db, review) is True
    db.refresh(review)
    assert sealed._claim(db, review) is False
    # Second runner returns without touching the verdict; the lock holder finishes once.
    sealed.run(db, review.id)
    assert db.get(Recheck, review.id).status == 'running'
    assert db.get(Recheck, review.id).verdict == ''


def test_original_conclusion_and_delivered_brief_untouched(db):
    job, _ = make_job(db)
    before = checksum(job.result)
    review = request_and_run(db, job)
    assert review.verdict == 'passed'
    db.refresh(job)
    assert checksum(job.result) == before
    assert job.approved is False
    assert job.result['summary']
