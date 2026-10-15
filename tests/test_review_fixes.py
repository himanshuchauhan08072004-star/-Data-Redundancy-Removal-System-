"""Regression tests for bugs found in the review + verification of DB guarantees."""
import collections
import contextlib
import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import deduplicator
from app.database import Base, SessionLocal, engine, _normalise_url
from app.main import app
from app.models import Record, init_db
from app.normalizer import norm_name, fingerprint

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    yield


def post(**kw):
    return client.post("/api/records", json=kw)


def upload(content, name="t.csv"):
    return client.post("/api/upload", files={"file": (name, content, "text/csv")})


# ---------- malformed / missing input ----------
def test_null_and_numeric_json_are_classified_and_audited():
    r = post(name=None, email="a@b.com")
    assert r.status_code == 422 and r.json()["classification"] == "invalid"
    r = post(name="Num Ber", email="num@b.com", phone=9876543210)
    assert r.status_code == 201
    assert len(client.get("/api/audit").json()) == 2


def test_missing_fields_give_specific_reasons():
    r = post(name="Only Name")
    assert r.status_code == 422 and r.json()["reason"] == "email is required"
    r = post()
    assert "name is required" in r.json()["reason"] and "email is required" in r.json()["reason"]


@pytest.mark.parametrize("email", ["a..b@x.com", ".a@x.com", "a.@x.com", "a@-x.com", "a@x", "a@@x.com",
                                   "a b@x.com", "+tag@x.com", "a@x.c"])
def test_malformed_emails_rejected(email):
    r = post(name="Mal Formed", email=email)
    assert r.status_code == 422 and r.json()["classification"] == "invalid", email


@pytest.mark.parametrize("phone", ["98765abc43210", "123456", "1" * 16, "98765 4321 0 #"])
def test_bad_phones_rejected(phone):
    assert post(name="Ph One", email="p@x.com", phone=phone).status_code == 422


@pytest.mark.parametrize("phone", ["+91 98765 43210", "(011) 2345-6789", "9876543210"])
def test_good_phones_accepted(phone):
    assert post(name="Ph One", email="p@x.com", phone=phone).status_code == 201


def test_digit_only_name_rejected():
    r = post(name="12345", email="d@x.com")
    assert r.status_code == 422 and "letters" in r.json()["reason"]


def test_length_limits_protect_db_columns():
    assert post(name="L E", email="a" * 300 + "@x.com").status_code == 422
    assert post(name="L P", email="lp@x.com", phone="9 " * 20).status_code == 422
    assert len(client.get("/api/records").json()) == 0


def test_audit_payload_is_truncated():
    post(name="N" * 5000, email="big@x.com")
    assert len(client.get("/api/audit").json()[0]["payload"]) < 1500


# ---------- unicode names ----------
def test_unicode_names_participate_in_detection():
    assert norm_name("राहुल शर्मा") != ""
    assert norm_name("José") == norm_name("Jose")
    post(name="राहुल शर्मा", email="h1@x.com", phone="9000000001")
    r = post(name="राहुल शर्मा", email="h2@other.org", phone="9000000002")
    assert r.json()["classification"] == "false_positive"
    r = post(name="प्रिया वर्मा", email="h3@other.org")
    assert r.json()["classification"] == "unique"


# ---------- paging + timestamps ----------
@pytest.mark.parametrize("q", ["limit=-1", "limit=0", "limit=501", "offset=-1"])
def test_paging_bounds(q):
    assert client.get("/api/records?" + q).status_code == 422
    assert client.get("/api/audit?" + q.replace("offset", "limit")).status_code == 422


def test_timestamps_are_timezone_aware_utc():
    post(name="Time Stamp", email="ts@x.com")
    for ts in (client.get("/api/records").json()[0]["created_at"], client.get("/api/audit").json()[0]["time"]):
        assert ts.endswith("+00:00"), ts


# ---------- CSV ----------
def test_csv_missing_required_column_rejected_clearly():
    r = upload("name,phone\nA B,12345678\n")
    assert r.status_code == 400 and "email" in r.json()["detail"]


def test_csv_semicolon_and_tab_delimiters():
    assert upload("name;email;phone\nSemi Colon;sc@x.com;9000000001\n").json()["inserted"] == 1
    assert upload("name\temail\nTab Sep\ttab@x.com\n").json()["inserted"] == 1


@pytest.mark.parametrize("content", ["", "   \n", bytes(range(256))])
def test_csv_empty_or_binary_is_400_not_500(content):
    assert upload(content).status_code == 400


def test_csv_short_and_extra_columns_and_bom():
    r = upload("\ufeffName,Email,Phone\nShort Row,sr@x.com\nExtra Col,ec@x.com,9000000002,zzz\n")
    assert r.status_code == 200 and r.json()["inserted"] == 2


def test_csv_header_only_ok_and_wrong_extension_rejected():
    assert upload("name,email,phone\n").json()["total"] == 0
    assert upload("a", name="t.txt").status_code == 400


def test_csv_every_row_processed_and_audited():
    rows = [("A One", "a1@x.com", "9100000001"), ("A One", "a1@x.com", "9100000001"), ("", "x@x.com", ""),
            ("B Two", "bad", ""), ("C Three", "c3@x.com", "12"), ("D Four", "d4@x.com", "")]
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(["name", "email", "phone"]); w.writerows(rows)
    d = upload(buf.getvalue()).json()
    assert d["total"] == 6 and len(d["results"]) == 6
    assert d["counts"] == {"unique": 2, "false_positive": 0, "redundant": 1, "invalid": 3}
    audit = client.get("/api/audit").json()
    assert len(audit) == 6
    assert collections.Counter(a["classification"] for a in audit) == collections.Counter(
        r["classification"] for r in d["results"])


# ---------- stats come from stored data ----------
def test_stats_match_database_and_audit():
    for i in range(3):
        post(name=f"Stat {chr(65 + i)}tester", email=f"s{i}@x.com", phone=f"900000000{i}")
    post(name="Stat Atester", email="s0@x.com")       # redundant
    post(name="", email="s9@x.com")                   # invalid
    s = client.get("/api/stats").json()
    with SessionLocal() as db:
        stored = db.query(Record).count()
    assert s["stored_records"] == stored == 3
    assert s["total_submitted"] == len(client.get("/api/audit").json()) == 5
    assert s["counts"]["redundant"] == 1 and s["counts"]["invalid"] == 1
    assert s["redundancy_rate"] == round(1 / 5, 4)


# ---------- DB-level guarantees ----------
def _dup_payloads(kind, n=20):
    if kind == "email":
        return [{"name": "Race A", "email": "race@x.com", "phone": ""}] * n
    if kind == "phone":
        return [{"name": f"Ph {chr(65 + i)}", "email": f"ph{i}@x.com", "phone": "9555000111"} for i in range(n)]
    return [{"name": "Near Dup", "email": f"near.dup{'x' * (i % 3)}@x.com", "phone": ""} for i in range(n)]


def _fire(payloads):
    def f(p):
        return client.post("/api/records", json=p).status_code
    with ThreadPoolExecutor(20) as ex:
        return collections.Counter(ex.map(f, payloads))


@pytest.mark.parametrize("kind,expected", [("email", 1), ("phone", 1), ("near", 1)])
def test_concurrent_duplicates_with_app_lock(kind, expected):
    codes = _fire(_dup_payloads(kind))
    with SessionLocal() as db:
        assert db.query(Record).count() == expected
    assert set(codes) <= {201, 409} and codes[201] == expected


@pytest.mark.parametrize("kind", ["email", "phone"])
def test_db_constraints_alone_stop_concurrent_duplicates(kind, monkeypatch):
    """App lock disabled: only the DB unique constraints stand between us and duplicates."""
    monkeypatch.setattr(deduplicator, "ingest_lock", contextlib.nullcontext())
    codes = _fire(_dup_payloads(kind))
    with SessionLocal() as db:
        assert db.query(Record).count() == 1
    assert set(codes) <= {201, 409}, codes


def test_unique_constraints_exist_and_block_direct_inserts():
    with SessionLocal() as db:
        db.add(Record(name="A", email="a@x.com", phone="9", name_norm="a", email_norm="a@x.com",
                      phone_norm="9123456789", fingerprint=fingerprint("a", "a@x.com", "9123456789")))
        db.commit()
        db.add(Record(name="B", email="b@x.com", phone="9", name_norm="b", email_norm="b@x.com",
                      phone_norm="9123456789", fingerprint="other"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # empty phones must NOT collide with each other
        for i in range(2):
            db.add(Record(name="C", email=f"c{i}@x.com", phone="", name_norm="c", email_norm=f"c{i}@x.com",
                          phone_norm="", fingerprint=f"fp{i}"))
        db.commit()


def test_init_db_upgrades_legacy_database_without_phone_index(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'old.db'}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("DROP INDEX ux_records_phone_norm"))
    assert "ux_records_phone_norm" not in {i["name"] for i in inspect(eng).get_indexes("records")}
    init_db(eng)
    assert "ux_records_phone_norm" in {i["name"] for i in inspect(eng).get_indexes("records")}


def test_postgres_url_scheme_normalised():
    assert _normalise_url("postgres://u:p@h/db") == "postgresql://u:p@h/db"
    assert _normalise_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
    assert _normalise_url("sqlite:///./x.db") == "sqlite:///./x.db"


def test_row_cap_enforced_for_csv_and_bulk():
    rows = "name,email\n" + "\n".join(f"Cap Row,c{i}@x.com" for i in range(2001))
    assert upload(rows).status_code == 413
    assert client.post("/api/records/bulk", json=[{"name": "A B", "email": "a@x.com"}] * 2001).status_code == 413
    assert len(client.get("/api/records").json()) == 0
