"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# tz_aware=True: PyMongo returns datetimes that know they are UTC (not naive)
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Indexes (created at startup, safe to run many times)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    # one employee per emp_code, enforced by the database itself
    db.employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined")
    # one record per employee per day, enforced by the database (stops double punch-in)
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date"
    )
    # the default list order: newest date first, then emp_code
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")
        # finds an employee's latest punch-in quickly (used by punch-out)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """API milliseconds -> UTC datetime, cut to whole seconds (rule R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime from MongoDB -> API milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar day this punch-in belongs to."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)  # early-morning punch belongs to yesterday's shift
    return day.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str):
    """Start and end of the shift for an attendance date, in IST."""
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if MORE than 10:00 after shift start; then whole minutes since shift start."""
    start, _ = shift_window(date_str, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only if at least 30."""
    _, end = shift_window(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: rounded work hours below 4.50."""
    return work_hours < 4.5


# --------------------------------------------------------------------------- #
# Turning MongoDB documents into API responses (no _id, times as milliseconds)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entries) -> list:
    out = []
    for e in entries or []:
        changes = {}
        for field, ch in (e.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {"from": dt_to_ms(ch.get("from")), "to": dt_to_ms(ch.get("to"))}
            else:
                changes[field] = {"from": ch.get("from"), "to": ch.get("to")}
        out.append(
            {"at": dt_to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes}
        )
    return out


def attendance_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# Request models (FastAPI rejects bad input with 422 before our code runs)
# --------------------------------------------------------------------------- #
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# whole number of milliseconds; rejects floats, strings, and seconds-sized numbers
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AnyStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # raises ValueError for 2026-02-31 -> becomes 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


class RegularizeIn(BaseModel):
    status: Optional[AnyStatus] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {
        "items": [employee_out(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(UTC).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique index (emp_code, date) makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(UTC).replace(microsecond=0)

    # the employee's most recent record that started at or before this punch-out
    # (works across midnight, because we look at punch_in times, not at dates)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")

    work_hours = compute_work_hours(rec["punch_in"], ts)
    # "punch_out: None" in the filter means: only update if it is STILL open.
    # If two requests race, only one can match; the other gets None back -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
                "half_day": compute_half_day(work_hours),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date: str = Path(pattern=DATE_RE)):
    # (here "date" is the URL text, e.g. "2026-07-06")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be a real date, YYYY-MM-DD")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for this date")

    shift_start, shift_end = emp["shift_start"], emp["shift_end"]
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    # 1) work out the final status and punch times
    new_status = body.status or old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        new_in, new_out = None, None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if attendance_date(new_in, shift_start, shift_end) != date:
            raise HTTPException(422, "punch_in must stay on this record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    # 2) if nothing the client can edit actually changes, refuse
    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        raise HTTPException(422, "this request changes nothing")

    # 3) recompute the derived fields from the final values
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    if new_in is None:
        new.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        new["late_minutes"] = compute_late_minutes(new_in, date, shift_start, shift_end)
        if new_out is None:
            new.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            wh = compute_work_hours(new_in, new_out)
            new["work_hours"] = wh
            new["overtime_minutes"] = compute_overtime(new_out, date, shift_start, shift_end)
            new["half_day"] = compute_half_day(wh)

    # 4) the audit entry: only the fields that really changed
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # 5) save only if the record is still exactly as we read it (compare-and-set),
    #    and APPEND to history with $push. A concurrent edit makes this match
    #    nothing, so the loser gets 409 instead of silently overwriting.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return attendance_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AnyStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]


def month_bounds(month: str):
    """'2026-07' -> (first day, last day) of that month."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "month year is out of range")
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def weekend_dates(first: date, last: date) -> list:
    """Saturdays and Sundays between two dates, as 'YYYY-MM-DD' strings."""
    out, d = [], first
    while d <= last:
        if d.weekday() >= 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days between two dates, both included (R7)."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    weekends = weekend_dates(first, last)
    return [
        # uses the (emp_code, date) index: one employee, one month
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                # presence status on a Mon-Fri: 0.5 for a half day, otherwise 1
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", PRESENCE]}, {"$eq": [{"$in": ["$date", weekends]}, False]}]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # working days run from the later of (1st of month, joined_on) to the end of the month
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(start, last)

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    s = rows[0] if rows else {}
    present_days = float(s.get("present_days", 0))

    pct = None
    if working_days > 0:
        raw = Decimal(str(present_days)) * 100 / Decimal(working_days)
        pct = float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(s.get("leave_days", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }