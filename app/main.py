import csv
import io
from pathlib import Path

from fastapi import FastAPI, Depends, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Record, AuditLog
from .deduplicator import ingest

Base.metadata.create_all(engine)

app = FastAPI(title="Data Redundancy Removal System", version="1.0.0")
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class RecordIn(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""


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
    if len(items) > 5000:
        raise HTTPException(413, "max 5000 records per request")
    return _summarise([ingest(db, i.name, i.email, i.phone) for i in items])


@app.post("/api/upload")
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "CSV file required (columns: name,email,phone)")
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if len(rows) > 5000:
        raise HTTPException(413, "max 5000 rows per upload")
    norm = lambda r: {(k or "").strip().lower(): (v or "") for k, v in r.items()}
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
def list_records(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    rows = db.scalars(select(Record).order_by(Record.id.desc()).limit(min(limit, 500)).offset(offset))
    return [{"id": r.id, "name": r.name, "email": r.email, "phone": r.phone,
             "flagged_false_positive": r.flagged_false_positive,
             "created_at": r.created_at.isoformat()} for r in rows]


@app.get("/api/audit")
def audit(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500)))
    return [{"id": a.id, "time": a.created_at.isoformat(), "classification": a.classification,
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
