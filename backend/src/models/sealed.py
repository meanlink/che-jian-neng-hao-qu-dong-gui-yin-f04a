from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class SealedTrial(Base):
    """Immutable evidence bundle written when an analysis trial completes."""
    __tablename__ = 'sealed_trials'
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'), unique=True, index=True)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    sample: Mapped[list] = mapped_column(JSON)
    sample_checksum: Mapped[str] = mapped_column(String(64))
    dataset_checksum: Mapped[str] = mapped_column(String(64))
    holdout: Mapped[list] = mapped_column(JSON)
    expected_days: Mapped[int] = mapped_column(Integer)
    material_checksum: Mapped[str] = mapped_column(String(64))
    sealed_at: Mapped[float] = mapped_column(Double)


class Recheck(Base):
    """One re-computation review per trial; retries resume this same record."""
    __tablename__ = 'rechecks'
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    verdict: Mapped[str] = mapped_column(String(20), default='')
    reason: Mapped[str] = mapped_column(String(1000), default='')
    algorithm_original: Mapped[str] = mapped_column(String(80), default='')
    algorithm_used: Mapped[str] = mapped_column(String(80), default='')
    expected_days: Mapped[int] = mapped_column(Integer, default=0)
    checked_days: Mapped[int] = mapped_column(Integer, default=0)
    material_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    locked_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class RecheckDay(Base):
    """Per-date comparison; unique per review so retries never double-register."""
    __tablename__ = 'recheck_days'
    __table_args__ = (UniqueConstraint('recheck_id', 'day', name='uq_recheck_day'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    recheck_id: Mapped[int] = mapped_column(ForeignKey('rechecks.id'), index=True)
    day: Mapped[str] = mapped_column(String(10))
    actual: Mapped[float | None] = mapped_column(Double, nullable=True)
    archived: Mapped[float | None] = mapped_column(Double, nullable=True)
    replayed: Mapped[float | None] = mapped_column(Double, nullable=True)
    delta: Mapped[float | None] = mapped_column(Double, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(300), default='')
