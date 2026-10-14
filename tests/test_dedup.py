import os
os.environ["DATABASE_URL"] = "sqlite:///./test.db"

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def post(name, email, phone=""):
    return client.post("/api/records", json={"name": name, "email": email, "phone": phone})


def test_unique_inserted():
    r = post("Rahul Sharma", "rahul@example.com", "9876543210")
    assert r.status_code == 201 and r.json()["classification"] == "unique"


def test_exact_duplicate_blocked():
    post("Rahul Sharma", "rahul@example.com", "9876543210")
    r = post("Rahul Sharma", "rahul@example.com", "9876543210")
    assert r.status_code == 409 and r.json()["classification"] == "redundant"


def test_gmail_dots_and_plus_normalised():
    post("Amit Kumar", "amit.kumar@gmail.com")
    r = post("Amit K", "amitkumar+promo@gmail.com")
    assert r.status_code == 409


def test_name_order_and_phone_format():
    post("Rahul Sharma", "a@x.com", "+91 98765 43210")
    r = post("Sharma Rahul", "b@y.com", "98765-43210")
    assert r.status_code == 409 and "phone" in r.json()["reason"]


def test_near_duplicate_blocked():
    post("Priya Singh", "priya.singh@example.com")
    r = post("Priya Sing", "priya.sing@example.com")
    assert r.status_code == 409 and "near duplicate" in r.json()["reason"]


def test_false_positive_inserted_and_flagged():
    post("Priya Singh", "priya@example.com", "9811122233")
    r = post("Priya Singh", "psingh@otherco.org", "9999988888")
    assert r.status_code == 201 and r.json()["classification"] == "false_positive"
    recs = client.get("/api/records").json()
    assert len(recs) == 2 and recs[0]["flagged_false_positive"] is True


@pytest.mark.parametrize("n,e,p", [("", "a@b.com", ""), ("X", "bad", ""), ("X", "a@b.com", "12")])
def test_invalid_rejected(n, e, p):
    r = post(n, e, p)
    assert r.status_code == 422 and r.json()["classification"] == "invalid"


def test_csv_upload_and_stats():
    with open("sample_data.csv", "rb") as f:
        r = client.post("/api/upload", files={"file": ("sample_data.csv", f, "text/csv")})
    d = r.json()
    assert d["total"] == 13 and d["counts"]["invalid"] == 3 and d["counts"]["redundant"] >= 4
    s = client.get("/api/stats").json()
    assert s["stored_records"] == d["inserted"]
    # no two stored records share an email
    emails = [x["email"].lower() for x in client.get("/api/records").json()]
    assert len(emails) == len(set(emails))


def test_audit_logs_every_decision():
    post("A B", "a@b.com"); post("A B", "a@b.com"); post("", "a@b.com")
    assert len(client.get("/api/audit").json()) == 3
