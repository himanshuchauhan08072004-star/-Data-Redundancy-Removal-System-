"""Core engine: classify incoming data, validate against existing data,
append only unique + verified entries.

Classification
  invalid         -> fails validation, rejected
  redundant       -> same entity already stored (exact or near duplicate), rejected
  false_positive  -> looked similar (name) but verification shows distinct
                     identifiers, so it is a different entity: inserted + flagged
  unique          -> no similar data, inserted
"""
import json
import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Record, AuditLog
from .normalizer import norm_name, norm_email, norm_phone, fingerprint

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9_%+\-]+(\.[A-Za-z0-9_%+\-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
PHONE_RE = re.compile(r"^\+?[\d\s\-().]+$")
MAX_NAME, MAX_EMAIL, MAX_PHONE = 200, 254, 32

NEAR_DUP_THRESHOLD = 0.90   # combined name+email similarity => redundant
NAME_SIM_FLAG = 0.85        # name similarity alone => candidate (false positive check)
# A near duplicate needs 0.5*name + 0.5*email >= 0.90, so name >= 0.80 is a hard floor.
# Rows below it can never match, so they are skipped without computing the email score.
MIN_NAME_SIM = min(NAME_SIM_FLAG, 2 * NEAR_DUP_THRESHOLD - 1)

# Serialises classify+insert inside one process so near-duplicate checks cannot race.
# DB unique constraints remain the guard across processes/instances.
ingest_lock = threading.Lock()


@dataclass
class Decision:
    classification: str
    action: str
    reason: str
    matched_id: int | None = None
    score: float = 0.0
    record_id: int | None = None

    def as_dict(self):
        return self.__dict__.copy()


def validate(name: str, email: str, phone: str) -> str | None:
    """Return all problems joined by '; ', or None if valid."""
    name, email, phone = (name or "").strip(), (email or "").strip(), (phone or "").strip()
    errors: list[str] = []

    if not name:
        errors.append("name is required")
    elif len(name) > MAX_NAME:
        errors.append(f"name too long (max {MAX_NAME})")
    elif not norm_name(name):
        errors.append("name must contain letters")

    if not email:
        errors.append("email is required")
    elif len(email) > MAX_EMAIL:
        errors.append(f"email too long (max {MAX_EMAIL})")
    elif not EMAIL_RE.match(email) or len(email.partition("@")[0]) > 64:
        errors.append("invalid email format")
    elif not norm_email(email).partition("@")[0]:
        errors.append("invalid email format (nothing before +tag)")

    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(phone) > MAX_PHONE:
            errors.append(f"phone too long (max {MAX_PHONE} characters)")
        elif not PHONE_RE.match(phone):
            errors.append("phone contains invalid characters")
        elif not 7 <= len(digits) <= 15:
            errors.append("phone must have 7-15 digits")

    return "; ".join(errors) or None


def classify(db: Session, name: str, email: str, phone: str) -> Decision:
    err = validate(name, email, phone)
    if err:
        return Decision("invalid", "rejected", err)

    n, e, p = norm_name(name), norm_email(email), norm_phone(phone)
    fp = fingerprint(n, e, p)

    # 1. exact fingerprint
    hit = db.scalar(select(Record).where(Record.fingerprint == fp))
    if hit:
        return Decision("redundant", "rejected", "exact duplicate (fingerprint match)", hit.id, 1.0)

    # 2. same email => same entity
    hit = db.scalar(select(Record).where(Record.email_norm == e))
    if hit:
        return Decision("redundant", "rejected", "email already registered", hit.id, 1.0)

    # 3. same phone => same entity
    if p:
        hit = db.scalar(select(Record).where(Record.phone_norm == p))
        if hit:
            return Decision("redundant", "rejected", "phone already registered", hit.id, 1.0)

    # 4. fuzzy scan: near duplicates + false-positive candidates.
    #    Only the 3 needed columns are loaded; seq2 is cached by SequenceMatcher.
    name_m = SequenceMatcher(autojunk=False)
    name_m.set_seq2(n)
    candidates = []  # (id, name_sim, email_norm)
    for rid, rname, remail in db.execute(select(Record.id, Record.name_norm, Record.email_norm)):
        if not rname:
            continue
        name_m.set_seq1(rname)
        if name_m.real_quick_ratio() < MIN_NAME_SIM or name_m.quick_ratio() < MIN_NAME_SIM:
            continue
        ns = name_m.ratio()
        if ns >= MIN_NAME_SIM:
            candidates.append((rid, ns, remail))

    email_m = SequenceMatcher(autojunk=False)
    email_m.set_seq2(e)
    best_id, best_combined = None, 0.0
    name_id, name_best = None, 0.0
    for rid, ns, remail in candidates:
        email_m.set_seq1(remail)
        combined = 0.5 * ns + 0.5 * email_m.ratio()
        if combined > best_combined:
            best_id, best_combined = rid, combined
        if ns > name_best:
            name_id, name_best = rid, ns

    if best_id is not None and best_combined >= NEAR_DUP_THRESHOLD:
        return Decision("redundant", "rejected",
                        f"near duplicate (similarity {best_combined:.2f})", best_id, round(best_combined, 3))

    if name_id is not None and name_best >= NAME_SIM_FLAG:
        # similar name, but email and phone both differ => verified different entity
        return Decision("false_positive", "inserted",
                        f"similar name (sim {name_best:.2f}) but distinct email/phone: verified unique",
                        name_id, round(name_best, 3))

    return Decision("unique", "inserted", "no similar data found", None, round(name_best, 3))


def _clip(s: str, n: int = 500) -> str:
    return s if len(s) <= n else s[:n] + "...[truncated]"


def ingest(db: Session, name: str, email: str, phone: str = "") -> Decision:
    name, email, phone = (name or "").strip(), (email or "").strip(), (phone or "").strip()
    payload = json.dumps({"name": _clip(name), "email": _clip(email), "phone": _clip(phone)})

    with ingest_lock:
        d = classify(db, name, email, phone)

        if d.action == "inserted":
            n, e, p = norm_name(name), norm_email(email), norm_phone(phone)
            rec = Record(name=name, email=email, phone=phone, name_norm=n, email_norm=e,
                         phone_norm=p, fingerprint=fingerprint(n, e, p),
                         flagged_false_positive=(d.classification == "false_positive"))
            db.add(rec)
            try:
                db.flush()
                d.record_id = rec.id
            except IntegrityError:
                # concurrent insert won the race: DB unique constraint caught the duplicate
                db.rollback()
                d = Decision("redundant", "rejected", "duplicate caught by DB constraint", None, 1.0)

        db.add(AuditLog(classification=d.classification, action=d.action, reason=d.reason,
                        matched_id=d.matched_id, score=d.score, payload=payload))
        db.commit()
    return d
