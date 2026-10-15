import csv
import io
from pathlib import Path

from datetime import timezone

from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .database import get_db
from .models import Record, AuditLog, init_db
from .deduplicator import ingest

init_db()

app = FastAPI(title="Data Redundancy Removal System", version="1.0.0")
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000  # fuzzy scan is O(n) per row; 5000 rows took ~75 s, too slow for cloud request timeouts


class RecordIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""

    @field_validator("name", "email", "phone", mode="before")
    @classmethod
    def _coerce(cls, v):
        # null / numbers must reach the validator so they are classified + audited,
        # not rejected by FastAPI before the audit log sees them
        return "" if v is None else str(v)


def iso_utc(dt):
    """SQLite returns naive datetimes; they are UTC. Always emit an explicit offset."""
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/records")
def add_record(body: RecordIn, db: Session = Depends(get_db)):
    d = ingest(db, body.name, body.email, body.phone)
    code = {"inserted": 201, "rejected": 409 if d.classification == "redundant" else 422}[d.action]
    return JSONResponse(d.as_dict(), status_code=code)


@app.post("/api/records/bulk")
def add_bulk(items: list[RecordIn], db: Session = Depends(get_db)):
    if len(items) > MAX_ROWS:
        raise HTTPException(413, f"max {MAX_ROWS} records per request")
    return _summarise([ingest(db, i.name, i.email, i.phone) for i in items])


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "CSV file required (columns: name,email,phone)")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (max 5 MB)")
    text = raw.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise HTTPException(400, "CSV is empty (header row required: name,email,phone)")

    header = text.splitlines()[0]
    delim = max(",;\t|", key=header.count)  # comma, semicolon, tab or pipe
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        fields = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
        missing = [c for c in ("name", "email") if c not in fields]
        if missing:
            raise HTTPException(400, f"missing required column(s): {', '.join(missing)}. "
                                     f"Found: {', '.join(fields) or 'none'}")
        rows = list(reader)
    except csv.Error as exc:
        raise HTTPException(400, f"unreadable CSV: {exc}")
    if len(rows) > MAX_ROWS:
        raise HTTPException(413, f"max {MAX_ROWS} rows per upload")

    def norm(r):
        return {(k or "").strip().lower(): v for k, v in r.items()
                if k is not None and isinstance(v, str)}

    results = []
    for r in map(norm, rows):
        results.append(ingest(db, r.get("name", ""), r.get("email", ""), r.get("phone", "")))
    return _summarise(results)


def _summarise(results):
    counts = {"unique": 0, "false_positive": 0, "redundant": 0, "invalid": 0}
    for r in results:
        counts[r.classification] += 1
    return {
        "total": len(results),
        "inserted": counts["unique"] + counts["false_positive"],
        "rejected": counts["redundant"] + counts["invalid"],
        "counts": counts,
        "results": [r.as_dict() for r in results],
    }


@app.get("/api/records")
def list_records(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                 db: Session = Depends(get_db)):
    rows = db.scalars(select(Record).order_by(Record.id.desc()).limit(limit).offset(offset))
    return [{"id": r.id, "name": r.name, "email": r.email, "phone": r.phone,
             "flagged_false_positive": r.flagged_false_positive,
             "created_at": iso_utc(r.created_at)} for r in rows]


@app.get("/api/audit")
def audit(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
    return [{"id": a.id, "time": iso_utc(a.created_at), "classification": a.classification,
             "action": a.action, "reason": a.reason, "matched_id": a.matched_id,
             "score": a.score, "payload": a.payload} for a in rows]


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    rows = db.execute(select(AuditLog.classification, func.count()).group_by(AuditLog.classification)).all()
    c = {k: 0 for k in ("unique", "false_positive", "redundant", "invalid")}
    c.update(dict(rows))
    total = sum(c.values())
    return {"total_submitted": total, "stored_records": db.scalar(select(func.count()).select_from(Record)),
            "counts": c,
            "redundancy_rate": round(c["redundant"] / total, 4) if total else 0.0}
