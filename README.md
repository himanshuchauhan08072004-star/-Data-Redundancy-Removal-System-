# CodeAlpha_DataRedundancyRemovalSystem

CodeAlpha Cloud Computing Internship, Task 1. A cloud-ready API + dashboard that classifies incoming data as **unique**, **false positive**, **redundant** or **invalid**, and appends only unique, verified entries to the database.

## How it works

Every submission goes through this pipeline (`app/deduplicator.py`):

1. **Validate**: name required, email format, phone 7-15 digits. Fail -> `invalid`, rejected.
2. **Normalise** (`app/normalizer.py`): lowercase, strip accents/punctuation, token-sort names, strip `+tags` and Gmail dots from emails, keep last 10 phone digits.
3. **Exact check**: SHA-256 fingerprint of the normalised record.
4. **Identifier check**: same email or same phone -> `redundant`.
5. **Fuzzy check**: combined name + email similarity >= 0.90 -> `redundant` (near duplicate).
6. **False-positive verification**: similar name (>= 0.85) but different email and phone -> verified as a different person, inserted and flagged `false_positive`.
7. Otherwise -> `unique`, inserted.
8. **DB constraints** (unique `fingerprint`, unique `email_norm`) catch race conditions from concurrent requests.
9. Every decision (accepted or rejected) is written to `audit_log`.

## Run locally (Windows PowerShell)

```powershell
cd CodeAlpha_DataRedundancyRemovalSystem
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 (dashboard) or http://127.0.0.1:8000/docs (Swagger).

Run tests:

```powershell
python -m pytest -q
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/records` | Add one record. 201 inserted, 409 redundant, 422 invalid |
| POST | `/api/records/bulk` | JSON array of records |
| POST | `/api/upload` | CSV upload (`name,email,phone`) |
| GET | `/api/records` | Stored unique records |
| GET | `/api/audit` | Decision log |
| GET | `/api/stats` | Counts + redundancy rate |
| GET | `/health` | Health check |

## Database

Default: SQLite (`data.db`). For cloud, set `DATABASE_URL` to PostgreSQL (e.g. AWS RDS, Supabase, Neon) and uncomment `psycopg2-binary` in `requirements.txt`:

```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## Deploy to the cloud

Docker image included.

```powershell
docker build -t redundancy-system .
docker run -p 8000:8000 -e DATABASE_URL=sqlite:///./data.db redundancy-system
```

AWS: push image to ECR, run on App Runner (port 8000, health check `/health`), set `DATABASE_URL` to an RDS PostgreSQL instance.
Render/Railway: connect the GitHub repo, start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## Demo

Upload `sample_data.csv` from the dashboard: 13 rows -> duplicates, near duplicates, false positives and invalid rows are each handled and logged.

## Limitation

The fuzzy scan compares against all stored rows (O(n)). For millions of rows, add blocking (e.g. by email domain or name prefix) or a trigram index in PostgreSQL (`pg_trgm`).
