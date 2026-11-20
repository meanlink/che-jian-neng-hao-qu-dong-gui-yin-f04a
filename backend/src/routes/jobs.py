from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Job, Recheck, SealedTrial
from src.middlewares.auth import current_user, can_write, admin_only
from src.schemas.job import JobInput
from src.services import jobs, sealed
from src.services.audit import record
from src.utils.serialization import job_dict
router=APIRouter(prefix='/jobs')


def _load(db,id):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    return j


def _seal_dict(row):
    if not row: return None
    return {'algorithm_version':row.algorithm_version,'params':row.params,'sample_days':len(row.sample),
            'sample_checksum':row.sample_checksum,'dataset_checksum':row.dataset_checksum,
            'expected_days':row.expected_days,'sealed_at':row.sealed_at}


def _verification(db,j):
    seal_row=db.scalar(select(SealedTrial).where(SealedTrial.job_id==j.id))
    review=db.scalar(select(Recheck).where(Recheck.job_id==j.id))
    return {'seal':_seal_dict(seal_row),'recheck':sealed.detail(db,review)}


@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(100))]


@router.post('',status_code=201)
def create(data:JobInput,db=Depends(get_db),u=Depends(can_write)):
    j=jobs.create(db,data,u); record(db,u.email,'job.create',{'id':j.id,'params':j.params})
    db.commit(); jobs.enqueue(j.id); return job_dict(j)


@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    j=_load(db,id)
    return {**job_dict(j),**_verification(db,j)}


@router.post('/{id}/approve')
def approve(id:int,db=Depends(get_db),u=Depends(admin_only)):
    j=_load(db,id)
    if j.status!='completed':raise HTTPException(409,'仅已完成结果可以批准')
    j.approved=True; record(db,u.email,'job.approve',{'id':id}); db.commit()
    return job_dict(j)


@router.post('/{id}/recheck',status_code=202)
def recheck(id:int,db=Depends(get_db),u=Depends(admin_only)):
    j=_load(db,id)
    if j.status!='completed':raise HTTPException(409,'仅已完成结果可以复核')
    review=sealed.requested(db,j,u.id)
    record(db,u.email,'job.recheck.request',{'id':id,'recheck':review.id,'status':review.status}); db.commit()
    return {'recheck':sealed.detail(db,review)}


@router.get('/{id}/recheck')
def recheck_detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    j=_load(db,id)
    review=db.scalar(select(Recheck).where(Recheck.job_id==j.id))
    seal_row=db.scalar(select(SealedTrial).where(SealedTrial.job_id==j.id))
    return {'seal':_seal_dict(seal_row),'recheck':sealed.detail(db,review)}
