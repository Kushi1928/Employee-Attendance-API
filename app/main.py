"""Employee attendance API. Run with: uvicorn app.main:app --port 8000"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.on_event("startup")
def ensure_indexes():
    _ensure_index(db.employees, [("emp_code", ASCENDING)], "employee_code_unique", unique=True)
    _ensure_index(db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], "employee_department_code")
    _ensure_index(db.employees, [("department", ASCENDING), ("joined_on", ASCENDING)], "employee_department_joined")
    _ensure_index(db.employees, [("joined_on", ASCENDING)], "employee_joined")
    _ensure_index(db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], "attendance_employee_date_unique", unique=True)
    _ensure_index(db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], "attendance_date_employee")
    _ensure_index(db.attendance_logs, [("date", ASCENDING), ("status", ASCENDING)], "attendance_date_status")
    _ensure_index(db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], "attendance_status_date_employee")


def _ensure_index(collection, keys, name, unique=False):
    """Reuse an equivalent existing index even if another tool named it differently."""
    expected_keys = list(keys)
    for existing in collection.list_indexes():
        if list(existing["key"].items()) == expected_keys:
            if unique and not existing.get("unique", False):
                raise RuntimeError(f"existing index {existing['name']} must be unique")
            return existing["name"]
    return collection.create_index(keys, unique=unique, name=name)


def _now():
    return datetime.now(UTC).replace(microsecond=0)


def _epoch(value: Optional[datetime]):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _instant(ms: Optional[int]):
    if ms is None:
        return None
    if isinstance(ms, bool) or not isinstance(ms, int) or not 100_000_000_000 <= ms <= 4_102_444_800_000:
        raise ValueError("timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)


def _round(value, places=2):
    unit = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(unit, rounding=ROUND_HALF_UP))


def _clock(value: str):
    h, m = map(int, value.split(":"))
    return time(h, m, tzinfo=IST)


def _shift_end(attendance_date: str, shift_start: str, shift_end: str):
    day = date.fromisoformat(attendance_date)
    start_t, end_t = _clock(shift_start), _clock(shift_end)
    overnight = end_t <= start_t
    # attendance_date is already normalized to the shift's start day by punch-in.
    end_day = day + (timedelta(days=1) if overnight else timedelta())
    return datetime.combine(end_day, end_t, tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local = punch_in.astimezone(IST)
    shift_day = local.date()
    if _clock(shift_end) <= _clock(shift_start) and local.timetz() < _clock(shift_end):
        shift_day -= timedelta(days=1)
    start = datetime.combine(shift_day, _clock(shift_start))
    delta = int((local - start).total_seconds())
    return delta // 60 if delta > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # Divide integer seconds as Decimal so exact half-hundredth-hour ties use half-up.
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    minutes = int((punch_out - _shift_end(attendance_date, shift_start, shift_end)).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def _record_out(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    for key in ("punch_in", "punch_out"):
        doc[key] = _epoch(doc.get(key))
    doc["history"] = doc.get("history") or []
    for entry in doc["history"]:
        entry["at"] = _epoch(entry.get("at"))
        for change in entry.get("changes", {}).values():
            if isinstance(change, dict):
                change["from"] = _epoch(change.get("from")) if isinstance(change.get("from"), datetime) else change.get("from")
                change["to"] = _epoch(change.get("to")) if isinstance(change.get("to"), datetime) else change.get("to")
    return doc


def _employee_out(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    doc["created_at"] = _epoch(doc.get("created_at"))
    return doc


def _valid_date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return parsed


def _month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    last = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat(), last


def _working_days(start: date, end: date):
    # Count weekdays arithmetically rather than querying or enumerating attendance documents.
    days = (end - start).days + 1
    weeks, rem = divmod(days, 7)
    result = weeks * 5
    result += sum(1 for offset in range(rem) if (start.weekday() + offset) % 7 < 5)
    return result


def _unwrap(raw):
    """Normalize a single-document aggregation result."""
    return raw[0] if raw else None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{
        "emp_code": "EMP9999",
        "name": "Test Employee",
        "email": "test.employee9999@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-10-13",
    }]})

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(StrictModel):
    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{
        "emp_code": "EMP9999",
        "status": "PRESENT",
    }]})

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, description="Epoch milliseconds; omit to use the current server time.")
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            _instant(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at must be an integer when supplied")
        return values


class PunchOutIn(StrictModel):
    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{
        "emp_code": "EMP9999",
    }]})

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, description="Epoch milliseconds; omit to use the current server time.")

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            _instant(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at must be an integer when supplied")
        return values


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            _instant(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_changes(cls, values):
        if isinstance(values, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in values and values[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return values


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = _now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_employee_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = _instant(body.punched_at) if body.punched_at is not None else _now()
    local = ts.astimezone(IST)
    shift_start_t, shift_end_t = _clock(emp["shift_start"]), _clock(emp["shift_end"])
    attendance_day = local.date()
    if shift_end_t <= shift_start_t and local.timetz() < shift_end_t:
        attendance_day -= timedelta(days=1)
    day = attendance_day.isoformat()
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return _record_out(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from:
        _valid_date(date_from)
    if date_to:
        _valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = _instant(body.punched_at) if body.punched_at is not None else _now()
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    seconds = (ts - doc["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 86400:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    work = compute_work_hours(doc["punch_in"], ts)
    changes = {"punch_out": ts, "work_hours": work,
               "overtime_minutes": compute_overtime(ts, doc["date"], emp["shift_start"], emp["shift_end"]),
               "half_day": work < 4.50}
    updated = db.attendance_logs.find_one_and_update({"_id": doc["_id"], "punch_out": None}, {"$set": changes}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return _record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    attendance_date = date
    _valid_date(attendance_date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if not doc:
        raise HTTPException(404, "attendance record not found")
    new_status = body.status if body.status is not None else doc["status"]
    if new_status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
    if new_status in {"ABSENT", "LEAVE"}:
        punch_in = punch_out = None
    else:
        punch_in = _instant(body.punch_in) if body.punch_in is not None else doc.get("punch_in")
        punch_out = _instant(body.punch_out) if body.punch_out is not None else doc.get("punch_out")
        if punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if punch_in.astimezone(IST).date().isoformat() != attendance_date and not (
            _clock(emp["shift_end"]) <= _clock(emp["shift_start"]) and
            punch_in.astimezone(IST).timetz() < _clock(emp["shift_end"]) and
            (punch_in.astimezone(IST).date() - timedelta(days=1)).isoformat() == attendance_date):
            raise HTTPException(422, "punch_in must belong to attendance date")
        if punch_out is not None and ((punch_out - punch_in).total_seconds() <= 0 or (punch_out - punch_in).total_seconds() > 86400):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(punch_in, emp["shift_start"], emp["shift_end"]) if punch_in else 0
    work = compute_work_hours(punch_in, punch_out) if punch_in and punch_out else None
    overtime = compute_overtime(punch_out, attendance_date, emp["shift_start"], emp["shift_end"]) if work is not None else 0
    half = work < 4.50 if work is not None else False
    final = {"status": new_status, "punch_in": punch_in, "punch_out": punch_out, "late_minutes": late,
             "work_hours": work, "overtime_minutes": overtime, "half_day": half}
    changes = {}
    for key, value in final.items():
        old = doc.get(key, {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}.get(key))
        if old != value:
            changes[key] = {"from": old, "to": value}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": _now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic compare-and-swap prevents lost corrections/history under concurrent PATCHes.
    expected = {k: doc.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day") if k in doc}
    expected["_id"] = doc["_id"]
    update = db.attendance_logs.find_one_and_update(expected, {"$set": final, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if update is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return _record_out(update)


def _monthly_pipeline(emp_code, first, last):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$set": {"_day": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}},
                   "_present": {"$in": ["$status", list(PRESENCE)]}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": ["$_present", {"$gte": ["$_day", 2]}, {"$lte": ["$_day", 6]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last, last_day = _month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working = _working_days(start, last_day) if start <= last_day else 0
    raw = _unwrap(list(db.attendance_logs.aggregate(_monthly_pipeline(emp_code, first, last)))) or {}
    present = float(raw.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": _round(present),
            "leave_days": raw.get("leave_days", 0), "late_count": raw.get("late_count", 0),
            "total_late_minutes": raw.get("total_late_minutes", 0), "total_overtime_minutes": raw.get("total_overtime_minutes", 0),
            "attendance_pct": _round(present / working * 100) if working else None}


def _department_pipeline(month, department=None):
    first, last, _ = _month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}},
            {"$set": {"_day": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}}}}], "as": "logs"}},
        {"$set": {"headcount": {"$cond": [{"$lte": ["$joined_on", last]}, 1, 0]}, "logs": {"$ifNull": ["$logs", []]}}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"}, "headcount": {"$max": 1},
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$gte": ["$logs._day", 2]}, {"$lte": ["$logs._day", 6]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                    "sum_work": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
                    "work_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": "$headcount"}, "present_days": {"$sum": "$present_days"},
                    "sum_work": {"$sum": "$sum_work"}, "work_n": {"$sum": "$work_n"}, "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$work_n", 0]}, {"$divide": [{"$trunc": {"$add": [{"$multiply": [{"$divide": ["$sum_work", "$work_n"]}, 100]}, 0.5]}}, 100]}, None]},
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    _month_bounds(month)
    items = list(db.employees.aggregate(_department_pipeline(month, department)))
    for item in items:
        item["present_days"] = _round(item["present_days"])
    return {"month": month, "items": items}


def _leaderboard_pipeline(month, department=None, limit=10):
    first, last, _ = _month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                 {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
                 {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    _month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(_leaderboard_pipeline(month, department, limit)))}


def _trend_pipeline(department, first, last):
    # $documents seeds the requested interval; $densify performs gap filling in MongoDB.
    # Seed range is generated arithmetically here only to enable an empty-log department to still yield rows.
    # The actual daily gap filling and rolling calculation remain aggregation stages.
    return [
        {"$documents": [{"date": datetime.combine(date.fromisoformat(first), time.min, tzinfo=UTC), "_seed": True}]},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$match": {"date": {"$gte": first, "$lte": last}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$set": {"date": {"$dateFromString": {"dateString": "$date"}}}},
            {"$project": {"date": 1, "status": 1, "late_minutes": 1, "half_day": 1, "_seed": {"$literal": False}}}] }},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(date.fromisoformat(first), time.min, tzinfo=UTC), datetime.combine(date.fromisoformat(last) + timedelta(days=1), time.min, tzinfo=UTC)]}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$set": {"date": "$_id", "date_text": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_text"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$date"}, [1, 7]]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$trunc": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = _valid_date(from_), _valid_date(to)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid trend range")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    pipeline = _trend_pipeline(department, start.isoformat(), end.isoformat())
    items = list(db.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["date"] = item["date"].date().isoformat() if isinstance(item["date"], datetime) else item["date"]
        # MongoDB $round uses ties-to-even; final API rates require decimal half-up.
        for key in ("present_count",):
            item[key] = _round(item.get(key, 0))
        for key in ("attendance_rate", "moving_avg_7d"):
            if item.get(key) is not None:
                item[key] = _round(item[key], 4)
    return {"department": department, "items": items}


def _explain(endpoint, params):
    if endpoint == "attendance_list":
        page, size = params.get("page", 1), params.get("page_size", 20)
        query = {}
        for key in ("emp_code", "status"):
            if params.get(key) is not None:
                query[key] = params[key]
        if params.get("date_from") or params.get("date_to"):
            query["date"] = {}
            if params.get("date_from"): query["date"]["$gte"] = params["date_from"]
            if params.get("date_to"): query["date"]["$lte"] = params["date_to"]
        explain = db.command("explain", {"find": "attendance_logs", "filter": query,
                                      "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*size, "limit": size}, verbosity="executionStats")
        return "attendance_logs", explain
    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"): raise HTTPException(422, "emp_code and month are required")
        first, last, _ = _month_bounds(params["month"])
        pipeline = _monthly_pipeline(params["emp_code"], first, last)
        coll = "attendance_logs"
    elif endpoint == "department_summary":
        if not params.get("month"): raise HTTPException(422, "month is required")
        _month_bounds(params["month"]); pipeline = _department_pipeline(params["month"], params.get("department")); coll = "employees"
    elif endpoint == "late_leaderboard":
        if not params.get("month"): raise HTTPException(422, "month is required")
        _month_bounds(params["month"]); pipeline = _leaderboard_pipeline(params["month"], params.get("department"), params.get("limit", 10)); coll = "attendance_logs"
    elif endpoint == "department_trend":
        if not params.get("department") or not params.get("from") or not params.get("to"): raise HTTPException(422, "department, from and to are required")
        start, end = _valid_date(params["from"]), _valid_date(params["to"])
        if end < start or (end-start).days+1 > 92: raise HTTPException(422, "invalid trend range")
        pipeline = _trend_pipeline(params["department"], start.isoformat(), end.isoformat()); coll = "attendance_logs"
    else:
        raise HTTPException(422, "unsupported endpoint")
    aggregate_target = 1 if endpoint == "department_trend" else coll
    explain = db.command("explain", {"aggregate": aggregate_target, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
    return coll, explain


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    params = locals()
    params["from"] = from_
    collection, output = _explain(endpoint, params)
    return {"endpoint": endpoint, "collection": collection, "explain": output}
