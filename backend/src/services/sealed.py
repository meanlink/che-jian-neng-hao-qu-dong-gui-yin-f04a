"""Seal completed trials and re-compute them from immutable evidence.

A sealed trial captures exactly what was analysed: the normalised sample, the
parameters, the algorithm version and the hold-out daily predictions.  A
recheck replays the *original* algorithm version over that evidence and
compares every hold-out date within a strict tolerance.  Rechecks are
resumable, idempotent per trial and safe to trigger concurrently.
"""
from datetime import date
import time

from sqlalchemy import select, update, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models import Dataset, Job, Recheck, RecheckDay, SealedTrial
from src.services import algorithms
from src.services.audit import record
from src.utils.serialization import checksum

TOLERANCE = 0.001          # absolute kWh allowed per hold-out day
LOCK_LEASE = 15            # seconds a claim stays exclusive
FIELDS = ('units', 'run_hours', 'temp_c', 'idle_hours', 'kwh')


def normalise(records):
    """Return the exact, sorted, numeric sample that the algorithm consumed."""
    rows = []
    for item in records:
        day = date.fromisoformat(str(item['day']))
        row = {'day': day.isoformat()}
        for key in FIELDS:
            row[key] = float(item[key])
        rows.append(row)
    rows.sort(key=lambda row: row['day'])
    return rows


def seal(db: Session, job: Job, dataset: Dataset, version=None):
    """Write the immutable evidence bundle for a completed trial (idempotent)."""
    existing = db.scalar(select(SealedTrial).where(SealedTrial.job_id == job.id))
    if existing:
        return existing
    holdout = (job.result or {}).get('holdout')
    if not isinstance(holdout, list) or not holdout:
        raise ValueError('分析结果缺少留出期逐日预测，无法封存复算证据')
    sample = normalise(dataset.records)
    version = version or algorithms.CURRENT_VERSION
    holdout_rows = [
        {'day': str(row['day']), 'actual': float(row['actual']), 'predicted': float(row['predicted'])}
        for row in holdout
    ]
    sealed = SealedTrial(
        job_id=job.id,
        algorithm_version=version,
        params=dict(job.params or {}),
        sample=sample,
        sample_checksum=checksum(sample),
        dataset_checksum=dataset.checksum,
        holdout=holdout_rows,
        expected_days=len(holdout_rows),
        material_checksum=checksum({'algorithm_version': version, 'params': job.params or {}, 'sample': sample, 'holdout': holdout_rows}),
        sealed_at=time.time(),
    )
    db.add(sealed)
    db.flush()
    return sealed


def _material_failures(sealed: SealedTrial, dataset: Dataset | None):
    problems = []
    if checksum(sealed.sample) != sealed.sample_checksum:
        problems.append('封存样本指纹与样本内容不一致')
    if dataset is None:
        problems.append('原始数据批次已不存在')
    elif dataset.checksum != sealed.dataset_checksum:
        problems.append('数据批次内容与封存时指纹不一致')
    if len(sealed.sample) < 2:
        problems.append('封存样本不足')
    if not isinstance(sealed.holdout, list) or not sealed.holdout:
        problems.append('封存记录缺少留出期逐日预测')
    elif len(sealed.holdout) != sealed.expected_days:
        problems.append('留出期预测条数与封存天数不一致')
    holdout_days = [str(row.get('day')) for row in (sealed.holdout or [])]
    if any(not day or day == 'None' for day in holdout_days):
        problems.append('留出期预测缺少日期')
    if len(set(holdout_days)) != len(holdout_days):
        problems.append('留出期预测存在重复日期')
    sample_days = [row.get('day') for row in sealed.sample]
    if len(set(sample_days)) != len(sample_days):
        problems.append('封存样本存在重复日期')
    return problems


def _get_or_create(db: Session, job: Job, user_id: int):
    existing = db.scalar(select(Recheck).where(Recheck.job_id == job.id))
    if existing:
        return existing, False
    recheck = Recheck(job_id=job.id, status='pending', created_by=user_id, created_at=time.time())
    db.add(recheck)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Recheck).where(Recheck.job_id == job.id))
        if not existing:
            raise
        return existing, False
    return recheck, True


def _claim(db: Session, recheck: Recheck):
    now = time.time()
    result = db.execute(
        update(Recheck)
        .where(
            Recheck.id == recheck.id,
            or_(
                Recheck.status == 'pending',
                and_(Recheck.status == 'running', Recheck.locked_at < now - LOCK_LEASE),
            ),
        )
        .values(status='running', locked_at=now)
    )
    db.commit()
    return result.rowcount == 1


def requested(db: Session, job: Job, user_id: int):
    """Ensure exactly one recheck record exists for a trial.

    The worker picks up ``pending`` records, so repeated requests, retries and
    concurrent callers converge on the same record and the same verdict.
    A completed verdict is final, except that an *unverifiable* one may be
    retried once the missing evidence is shipped again.
    """
    recheck, _ = _get_or_create(db, job, user_id)
    if recheck.status == 'completed' and recheck.verdict == 'unverifiable':
        recheck.status = 'pending'
        recheck.verdict = ''
        recheck.reason = ''
        recheck.checked_days = 0
        recheck.material_ok = None
        recheck.locked_at = None
        recheck.finished_at = None
        record(db, 'recheck', 'recheck.reopened', {'job': job.id})
        db.commit()
    return recheck


def run(db: Session, recheck_id: int):
    recheck = db.get(Recheck, recheck_id)
    if not recheck or recheck.status == 'completed':
        return recheck
    if not _claim(db, recheck):
        return db.get(Recheck, recheck_id)
    db.refresh(recheck)
    record(db, 'recheck', 'recheck.start', {'job': recheck.job_id})
    db.commit()
    try:
        _execute(db, recheck)
    except Exception as exc:  # interrupted or unexpected: keep checkpoints, mark pending
        db.rollback()
        recheck = db.get(Recheck, recheck_id)
        recheck.status = 'pending'
        recheck.locked_at = None
        recheck.reason = ('复核中断：' + str(exc))[:1000]
        record(db, 'recheck', 'recheck.interrupted', {'job': recheck.job_id, 'error': str(exc)[:300]})
        db.commit()
        raise
    return db.get(Recheck, recheck_id)


def _finish(db: Session, recheck: Recheck, verdict: str, reason: str = ''):
    recheck.status = 'completed'
    recheck.verdict = verdict
    recheck.reason = reason[:1000]
    recheck.locked_at = None
    recheck.finished_at = time.time()
    record(db, 'recheck', 'recheck.' + verdict, {'job': recheck.job_id, 'reason': reason[:300]})
    db.commit()


def _execute(db: Session, recheck: Recheck):
    job = db.get(Job, recheck.job_id)
    sealed = db.scalar(select(SealedTrial).where(SealedTrial.job_id == recheck.job_id))
    if job is None:
        _finish(db, recheck, 'unverifiable', '原分析任务已不存在')
        return
    if sealed is None:
        _finish(db, recheck, 'unverifiable', '该历史试验未封存样本、参数、算法版本与留出期预测，缺少复算证据')
        return
    recheck.algorithm_original = sealed.algorithm_version
    recheck.expected_days = sealed.expected_days
    db.commit()
    if sealed.algorithm_version not in algorithms.available():
        recheck.algorithm_used = ''
        _finish(db, recheck, 'unverifiable', f'原算法版本 {sealed.algorithm_version} 已不在当前应用中，无法复算')
        return
    recheck.algorithm_used = sealed.algorithm_version
    db.commit()

    dataset = db.get(Dataset, job.dataset_id)
    problems = _material_failures(sealed, dataset)
    recheck.material_ok = not problems
    if problems:
        _finish(db, recheck, 'failed', '材料校验失败：' + '；'.join(problems))
        return
    db.commit()

    try:
        replayed = algorithms.run(sealed.algorithm_version, sealed.sample, sealed.params)
        replay_rows = list(replayed.get('holdout') or [])
    except Exception as exc:
        _finish(db, recheck, 'failed', ('原算法复算失败：' + str(exc))[:500])
        return
    replay_by_day = {str(row['day']): row for row in replay_rows}
    sample_days = {row['day'] for row in sealed.sample}
    expected_days = [str(row['day']) for row in sealed.holdout]
    checked = {row.day: row for row in db.scalars(select(RecheckDay).where(RecheckDay.recheck_id == recheck.id))}

    for expected in sealed.holdout:
        day = str(expected['day'])
        if day in checked:
            continue
        replay = replay_by_day.get(day)
        note = ''
        if day not in sample_days:
            ok, delta, predicted = False, None, None
            note = '封存样本缺少该日期'
        elif replay is None:
            ok, delta, predicted = False, None, None
            note = '重算结果缺少该日期'
        else:
            predicted = float(replay['predicted'])
            delta = abs(predicted - float(expected['predicted']))
            ok = delta <= TOLERANCE
            if not ok:
                note = f'预测偏差 {delta:.6f} kWh，超过 {TOLERANCE} kWh'
        db.add(RecheckDay(
            recheck_id=recheck.id, day=day, actual=float(expected['actual']),
            archived=float(expected['predicted']), replayed=predicted, delta=delta, ok=ok, note=note,
        ))
        recheck.locked_at = time.time()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        checked[day] = True
        recheck.checked_days = (recheck.checked_days or 0) + 1
        db.commit()

    missing = [day for day in replay_by_day if day not in set(expected_days)]
    rows = list(db.scalars(select(RecheckDay).where(RecheckDay.recheck_id == recheck.id).order_by(RecheckDay.day)))
    bad = [row for row in rows if not row.ok]
    recheck.checked_days = len(rows)
    if bad:
        dates = '、'.join(row.day for row in bad[:12])
        extra = '' if len(bad) <= 12 else f' 等 {len(bad)} 天'
        _finish(db, recheck, 'failed', f'存在 {len(bad)} 个差异日期：{dates}{extra}')
    elif missing:
        _finish(db, recheck, 'failed', '重算结果包含封存留出期之外的日期：' + '、'.join(sorted(missing)[:12]))
    elif len(rows) != sealed.expected_days:
        _finish(db, recheck, 'failed', f'仅核对 {len(rows)} 天，封存留出期为 {sealed.expected_days} 天')
    else:
        _finish(db, recheck, 'passed', f'{len(rows)} 个留出期日期逐日预测偏差均不超过 {TOLERANCE} kWh')


def summary(recheck: Recheck | None):
    if not recheck:
        return None
    return {
        'id': recheck.id, 'status': recheck.status, 'verdict': recheck.verdict,
        'reason': recheck.reason, 'algorithm_original': recheck.algorithm_original,
        'algorithm_used': recheck.algorithm_used, 'expected_days': recheck.expected_days,
        'checked_days': recheck.checked_days, 'material_ok': recheck.material_ok,
        'created_at': recheck.created_at, 'finished_at': recheck.finished_at,
    }


def detail(db: Session, recheck: Recheck | None):
    if not recheck:
        return None
    data = summary(recheck)
    rows = list(db.scalars(select(RecheckDay).where(RecheckDay.recheck_id == recheck.id).order_by(RecheckDay.day)))
    data['days'] = [
        {'day': row.day, 'actual': row.actual, 'archived': row.archived, 'replayed': row.replayed,
         'delta': row.delta, 'ok': row.ok, 'note': row.note}
        for row in rows
    ]
    data['differences'] = [row.day for row in rows if not row.ok]
    return data


def recover(db: Session):
    """Release claims left behind by an interrupted process so retries resume."""
    now = time.time()
    stale = list(db.scalars(select(Recheck).where(Recheck.status == 'running', Recheck.locked_at < now - LOCK_LEASE)))
    for recheck in stale:
        recheck.status = 'pending'
        recheck.locked_at = None
        record(db, 'worker', 'recheck.recovered', {'id': recheck.id, 'checked': recheck.checked_days})
    if stale:
        db.commit()
    return len(stale)
