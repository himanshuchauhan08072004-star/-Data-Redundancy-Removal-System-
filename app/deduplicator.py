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
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Record, AuditLog
from .normalizer import norm_name, norm_email, norm_phone, fingerprint

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
NEAR_DUP_THRESHOLD = 0.90   # combined name+email similarity => redundant
NAME_SIM_FLAG = 0.85        # name similarity alone => candidate (false positive check)


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


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def validate(name: str, email: str, phone: str) -> str | None:
    if not (name or "").strip():
        return "name is required"
    if len(name) > 200:
        return "name too long"
    if not EMAIL_RE.match((email or "").strip()):
        return "invalid email format"
    digits = re.sub(r"\D", "", phone or "")
    if phone and not (7 <= len(digits) <= 15):
        return "phone must have 7-15 digits"
    return None


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

    # 4. fuzzy scan: near duplicates + false-positive candidates
    best, best_score = None, 0.0
    name_match, name_match_sim = None, 0.0
    for r in db.scalars(select(Record)):
        ns = _sim(n, r.name_norm)
        combined = 0.5 * ns + 0.5 * _sim(e, r.email_norm)
        if combined > best_score:
            best, best_score = r, combined
        if ns > name_match_sim:
            name_match, name_match_sim = r, ns

    if best and best_score >= NEAR_DUP_THRESHOLD:
        return Decision("redundant", "rejected",
                        f"near duplicate (similarity {best_score:.2f})", best.id, round(best_score, 3))

    if name_match and name_match_sim >= NAME_SIM_FLAG:
        # similar name, but email and phone both differ => verified different entity
        return Decision("false_positive", "inserted",
                        f"similar name (sim {name_match_sim:.2f}) but distinct email/phone: verified unique",
                        name_match.id, round(name_match_sim, 3))

    return Decision("unique", "inserted", "no similar data found", None, round(best_score, 3))


def ingest(db: Session, name: str, email: str, phone: str = "") -> Decision:
    name, email, phone = (name or "").strip(), (email or "").strip(), (phone or "").strip()
    d = classify(db, name, email, phone)
    payload = json.dumps({"name": name, "email": email, "phone": phone})

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
            # concurrent insert won the race: DB constraint caught the duplicate
            db.rollback()
            d = Decision("redundant", "rejected", "duplicate caught by DB constraint", None, 1.0)

    db.add(AuditLog(classification=d.classification, action=d.action, reason=d.reason,
                    matched_id=d.matched_id, score=d.score, payload=payload))
    db.commit()
    return d
