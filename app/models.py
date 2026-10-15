from datetime import datetime, timezone
import logging
from sqlalchemy import String, Integer, Float, DateTime, Text, Boolean, Index, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base, engine as _default_engine

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


class Record(Base):
    """Only unique, verified entries live here. DB-level unique constraints
    are the final guard against race conditions."""
    __tablename__ = "records"
    __table_args__ = (
        # Same non-empty phone can never be stored twice, even under concurrent requests.
        Index("ux_records_phone_norm", "phone_norm", unique=True,
              sqlite_where=text("phone_norm <> ''"), postgresql_where=text("phone_norm <> ''")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    phone: Mapped[str] = mapped_column(String(32), default="")
    name_norm: Mapped[str] = mapped_column(String(200), index=True)
    email_norm: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    phone_norm: Mapped[str] = mapped_column(String(32), default="")
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


def init_db(eng=None):
    """Create tables and make sure the phone uniqueness index exists, including on
    databases created by an earlier version (create_all does not add indexes to
    existing tables)."""
    eng = eng or _default_engine
    Base.metadata.create_all(eng)
    try:
        with eng.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_records_phone_norm "
                              "ON records (phone_norm) WHERE phone_norm <> ''"))
    except SQLAlchemyError as exc:  # e.g. legacy data already holds duplicate phones
        log.warning("could not create ux_records_phone_norm: %s", exc)
