# Employee Attendance and Analytics API

A FastAPI service backed by MongoDB. It stores employees and one attendance record per employee per IST attendance day. It supports punch-in/out, audited HR corrections, paginated searches, monthly and department analytics, late ranking, daily trends, and MongoDB explain output. All application code is in `app/main.py` as requested.

## Run locally

Requirements: Python 3.11+, Git, and MongoDB 6.0+. Start MongoDB locally or use an Atlas connection string. The commands below are for **Command Prompt (cmd.exe)**:

```bat
python --version
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
python sample_seed.py
python -m uvicorn app.main:app --port 8000 --reload
```

Use any installed Python version 3.11 or newer; `py -3.11` only works if Python 3.11 is installed. In PowerShell, activate with `.\.venv\Scripts\Activate.ps1` and set variables with `$env:MONGO_URI="mongodb://localhost:27017"` and `$env:MONGO_DB="attendance_db"`. For Atlas, set `MONGO_URI` to the Atlas connection string and `MONGO_DB` to the chosen database before running the seed script or server. Environment variables take precedence over a local `.env`. The seed script clears and reloads the two sample collections, so use it only with a database you are comfortable resetting. Indexes are created automatically when the app starts; an equivalent existing index is reused even if it has a different name.

Open `http://127.0.0.1:8000/docs` for interactive API documentation. `GET /health` checks that MongoDB responds.

## Verify the API

With MongoDB running and the project dependencies installed, run the integration suite from the repository root:

```bat
python -m unittest discover -s tests -v
```

The suite connects to `MONGO_URI` (default `mongodb://localhost:27017`), creates a uniquely named temporary database, exercises the API and query plans, then drops that test database. Do not point `MONGO_URI` at a production server.

## What the API does

- `POST /employees`, `GET /employees`: create employees and return a stable, paginated directory.
- `POST /attendance/punch-in`, `POST /attendance/punch-out`: record attendance using IST dates and epoch-millisecond instants. Database uniqueness makes punch-in race-safe; conditional updates make punch-out race-safe.
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`: search records and apply a manual correction. Every actual correction adds one audit-history entry.
- `GET /analytics/employees/{emp_code}/monthly`: summarize weekdays, presence, leave, late minutes, and overtime for a month.
- `GET /analytics/departments/summary`: aggregate department headcount and attendance, including employees with no logs.
- `GET /analytics/leaderboard/late`: rank monthly late minutes with competition ties.
- `GET /analytics/departments/{department}/trend`: fill a daily range in MongoDB and report attendance rates plus a seven-day moving average.
- `GET /admin/explain/{endpoint}`: return MongoDB execution statistics for a supported endpoint query.

The service stores instants as UTC BSON dates and exposes them as epoch milliseconds. Calendar dates and shift times remain strings. Stored derived fields are used by analytics. The API has no authentication because the assignment contract does not include it.

## Project files

- `openapi.yaml`: request, response, status-code, and business-rule contract.
- `DATA_MODEL.md`: MongoDB collections and data conventions.
- `REVIEW.md`: starter defects and their fixes.
- `DECISIONS.md`: index, concurrency, ranking, headcount, and scale decisions.
