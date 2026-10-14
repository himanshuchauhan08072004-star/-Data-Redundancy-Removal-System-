from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, DateTime, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


def _now():
    return datetime.now(timezone.utc)


class Record(Base):
    """Only unique, verified entries live here. DB-level unique constraints
    are the final guard against race conditions."""
    __tablename__ = "records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    phone: Mapped[str] = mapped_column(String(32), default="")
    name_norm: Mapped[str] = mapped_column(String(200), index=True)
    email_norm: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    phone_norm: Mapped[str] = mapped_column(String(32), default="", index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    flagged_false_positive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    """Every decision (accepted or rejected) is recorded."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    classification: Mapped[str] = mapped_column(String(20))  # unique|false_positive|redundant|invalid
    action: Mapped[str] = mapped_column(String(20))          # inserted|rejected
    reason: Mapped[str] = mapped_column(Text)
    matched_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[str] = mapped_column(Text)
