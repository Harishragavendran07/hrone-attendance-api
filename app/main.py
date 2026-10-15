"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi import Path


from pydantic import BaseModel
from pymongo import MongoClient
from typing import Any, Optional
from pymongo.errors import PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

from pymongo.errors import DuplicateKeyError


@app.on_event("startup")
def create_indexes():
    client.admin.command("ping")

    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_emp_code",
    )

    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date",
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp",
    )

    
db.employees.create_index(
    [("joined_on", 1), ("department", 1)],
    name="ix_employee_joined_department",
)



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    local_time = (
        punch_in.astimezone(IST)
        if punch_in.tzinfo
        else punch_in.replace(tzinfo=IST)
    )

    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    shift_start_time = local_time.replace(
        hour=start_hour,
        minute=start_minute,
        second=0,
        microsecond=0,
    )

    if (
        (end_hour, end_minute) <= (start_hour, start_minute)
        and (local_time.hour, local_time.minute) < (end_hour, end_minute)
    ):
        shift_start_time -= timedelta(days=1)

    elapsed_seconds = (local_time - shift_start_time).total_seconds()

    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)

    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def count_working_days(start_date: Date, end_date: Date) -> int:
    if start_date > end_date:
        return 0

    total_days = (end_date - start_date).days + 1
    full_weeks, remaining_days = divmod(total_days, 7)
    working_days = full_weeks * 5

    for offset in range(remaining_days):
        current_weekday = (start_date.weekday() + offset) % 7
        if current_weekday < 5:
            working_days += 1

    return working_days

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None



class AttendanceCorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()

    if not re.fullmatch(r"EMP\d{4,6}", doc["emp_code"]):
        raise HTTPException(
            status_code=422,
            detail="emp_code must be EMP followed by 4 to 6 digits",
        )

    if not doc["name"].strip() or len(doc["name"]) > 100:
        raise HTTPException(422, "Invalid employee name")

    if (
        len(doc["email"]) > 120
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", doc["email"])
    ):
        raise HTTPException(422, "Invalid email address")

    if not doc["department"].strip() or len(doc["department"]) > 50:
        raise HTTPException(422, "Invalid department")

    time_pattern = r"^([01]\d|2[0-3]):[0-5]\d$"

    for field in ("shift_start", "shift_end"):
        if not re.fullmatch(time_pattern, doc[field]):
            raise HTTPException(422, f"Invalid {field}; expected HH:MM")

    if doc["shift_start"] == doc["shift_end"]:
        raise HTTPException(422, "Shift start and end must differ")

    try:
        Date.fromisoformat(doc["joined_on"])
    except ValueError:
        raise HTTPException(422, "joined_on must be a valid YYYY-MM-DD date")

    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for employee in items:
        created_at = employee.get("created_at")
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            employee["created_at"] = int(created_at.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY"}
    if body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Convert epoch milliseconds to an aware IST datetime.
    if body.punched_at is None:
        ts = datetime.now(IST)
    else:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or not 100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be epoch milliseconds",
            )

        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).astimezone(IST)

    ts = ts.replace(microsecond=0)

    attendance_date = ts.date().isoformat()

    stored_ts = ts.astimezone(timezone.utc).replace(tzinfo=None)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": stored_ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
    ts, emp["shift_start"], emp["shift_end"]
),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)

    doc["punch_in"] = int(ts.timestamp() * 1000)

    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or not 100000000000 <= body.punched_at <= 4102444800000
        ):
            raise HTTPException(422, "punched_at must be epoch milliseconds")

        ts = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        ).replace(microsecond=0)

    stored_ts = ts.replace(tzinfo=None)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": stored_ts},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_utc = record["punch_in"].replace(tzinfo=timezone.utc)
    elapsed_seconds = (ts - punch_in_utc).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = float(
        (Decimal(str(elapsed_seconds)) / Decimal("3600")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )

    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))

    shift_end = datetime.combine(
        Date.fromisoformat(record["date"]),
        time(end_h, end_m),
        tzinfo=IST,
    )

    if (end_h, end_m) <= (start_h, start_m):
        shift_end += timedelta(days=1)

    overtime_minutes = max(
        0,
        int((ts.astimezone(IST) - shift_end).total_seconds() // 60),
    )

    if overtime_minutes < 30:
        overtime_minutes = 0

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": stored_ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    assert updated is not None
    updated.pop("_id", None)
    updated["punch_in"] = int( # type: ignore
        updated["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000 # type: ignore
    )
    updated["punch_out"] = int( # type: ignore
        updated["punch_out"].replace(tzinfo=timezone.utc).timestamp() * 1000 # type: ignore
    )

    return updated





@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    for field, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                Date.fromisoformat(value)
                if len(value) != 10:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} must be a valid YYYY-MM-DD date",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )


    for doc in docs:
        for field in ("punch_in", "punch_out"):
            value = doc.get(field)
            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                doc[field] = int(value.timestamp() * 1000)

        doc.setdefault("history", [])
        doc.setdefault("half_day", False)

        for entry in doc["history"]:
            value = entry.get("at")
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                entry["at"] = int(value.timestamp() * 1000)

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    body: AttendanceCorrectionIn,
    attendance_date: str = Path(alias="date"),
):

    try:
        parsed_date = Date.fromisoformat(attendance_date)
        if parsed_date.isoformat() != attendance_date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    changes_requested = body.model_dump(exclude_unset=True)

    reason = changes_requested.pop("reason", "").strip()
    regularized_by = changes_requested.pop("regularized_by", "").strip()

    if not reason or not regularized_by:
        raise HTTPException(422, "reason and regularized_by are required")

    if len(reason) > 500 or len(regularized_by) > 100:
        raise HTTPException(422, "reason or regularized_by is too long")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
    {"emp_code": emp_code, "date": attendance_date}
)
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

    new_status = changes_requested.get("status", record["status"])

    if new_status not in allowed_statuses:
        raise HTTPException(422, "Invalid attendance status")

    updates = {}
    history_changes = {}

    for field in ("status", "punch_in", "punch_out"):
        if field not in changes_requested:
            continue

        value = changes_requested[field]

        if field in ("punch_in", "punch_out") and value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 100000000000 <= value <= 4102444800000
            ):
                raise HTTPException(422, f"{field} must be epoch milliseconds")

            value = datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).replace(microsecond=0, tzinfo=None)

        old_value = record.get(field)

        if isinstance(old_value, datetime):
            old_history_value = old_value.isoformat()
        else:
            old_history_value = old_value

        if isinstance(value, datetime):
            new_history_value = value.isoformat()
        else:
            new_history_value = value

        if old_value != value:
            updates[field] = value
            history_changes[field] = {
                "from": old_history_value,
                "to": new_history_value,
            }

    final_status = updates.get("status", record["status"])
    final_punch_in = updates.get("punch_in", record.get("punch_in"))
    final_punch_out = updates.get("punch_out", record.get("punch_out"))

    if final_status in {"ABSENT", "LEAVE"}:
        if (
            "punch_in" in changes_requested and changes_requested["punch_in"] is not None
        ) or (
            "punch_out" in changes_requested and changes_requested["punch_out"] is not None
        ):
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

        final_punch_in = None
        final_punch_out = None

    if final_status in {"PRESENT", "WFH", "ON_DUTY"} and final_punch_in is None:
        raise HTTPException(422, "Presence statuses require punch_in")

    if final_punch_in is not None:
        punch_in_utc = final_punch_in.replace(tzinfo=timezone.utc)
        punch_in_ist = punch_in_utc.astimezone(IST)

        if punch_in_ist.date() != parsed_date:
            raise HTTPException(422, "punch_in must belong to the attendance date")

    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        punch_in_utc = final_punch_in.replace(tzinfo=timezone.utc)
        punch_out_utc = final_punch_out.replace(tzinfo=timezone.utc)
        elapsed = (punch_out_utc - punch_in_utc).total_seconds()

        if elapsed <= 0 or elapsed > 86400:
            raise HTTPException(422, "Invalid punch-out time")

    if final_status in {"ABSENT", "LEAVE"}:
        final_punch_in = None
        final_punch_out = None

    for field, value in (
        ("punch_in", final_punch_in),
        ("punch_out", final_punch_out),
    ):
        old_value = record.get(field)

        if old_value != value:
            old_history_value = old_value.isoformat() if isinstance(old_value, datetime) else old_value
            new_history_value = value.isoformat() if isinstance(value, datetime) else value
            updates[field] = value
            history_changes[field] = {
                "from": old_history_value,
                "to": new_history_value,
            }

    if final_punch_in is None:
        updates["work_hours"] = None
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
    else:
        punch_in_utc = final_punch_in.replace(tzinfo=timezone.utc)
        punch_in_ist = punch_in_utc.astimezone(IST)

        updates["late_minutes"] = compute_late_minutes(
            punch_in_ist,
            emp["shift_start"],
            emp["shift_end"],
        )

        if final_punch_out is None:
            updates["work_hours"] = None
            updates["overtime_minutes"] = 0
            updates["half_day"] = False
        else:
            punch_out_utc = final_punch_out.replace(tzinfo=timezone.utc)
            seconds = (punch_out_utc - punch_in_utc).total_seconds()

            work_hours = float(
                (Decimal(str(seconds)) / Decimal("3600")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )

            start_hour, start_minute = map(int, emp["shift_start"].split(":"))
            end_hour, end_minute = map(int, emp["shift_end"].split(":"))

            shift_end = datetime.combine(
                parsed_date,
                time(end_hour, end_minute),
                tzinfo=IST,
            )

            if (end_hour, end_minute) <= (start_hour, start_minute):
                shift_end += timedelta(days=1)

            overtime = max(
                0,
                int(
                    (
                        punch_out_utc.astimezone(IST) - shift_end
                    ).total_seconds() // 60
                ),
            )

            updates["work_hours"] = work_hours
            updates["overtime_minutes"] = overtime if overtime >= 30 else 0
            updates["half_day"] = work_hours < 4.50

    if not history_changes:
        raise HTTPException(422, "No changes provided")

    history_changes.update({
        field: {
            "from": record.get(field),
            "to": updates.get(field),
        }
        for field in ("late_minutes", "work_hours", "overtime_minutes", "half_day")
        if record.get(field) != updates.get(field, record.get(field))
    })

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": regularized_by,
        "reason": reason,
        "changes": history_changes,
    }

    
    concurrency_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }

    if "history" in record:
        concurrency_filter["history"] = record["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        concurrency_filter,
        {
            "$set": updates,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed; retry the correction")

    
    updated = db.attendance_logs.find_one(
    {"_id": record["_id"]},
    {"_id": 0},
)

    if updated is None:
        raise HTTPException(
        status_code=404,
        detail="Attendance record not found",
    )

    updated.setdefault("history", [])
    updated.setdefault("half_day", False)

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    for entry in updated["history"]:
        value = entry.get("at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            entry["at"] = int(value.timestamp() * 1000)

    return updated


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
):
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1}
    )

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_number = map(int, month.split("-"))
    month_start = Date(year, month_number, 1)

    if month_number == 12:
        next_month = Date(year + 1, 1, 1)
    else:
        next_month = Date(year, month_number + 1, 1)

    month_end = next_month - timedelta(days=1)

    joined_on = Date.fromisoformat(employee["joined_on"])
    working_start = max(month_start, joined_on)

    working_days = count_working_days(working_start, month_end)

    
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat()
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            2
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            6
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$half_day", False]},
                                            True
                                        ]
                                    },
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    result = next(db.attendance_logs.aggregate(pipeline), {})

    present_days = result.get("present_days", 0)
    attendance_pct = None

    if working_days > 0:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                / Decimal(working_days)
                * Decimal("100")
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None,
):
    year, month_number = map(int, month.split("-"))

    if year < 1 or year > 9998:
        raise HTTPException(422, "Invalid month")

    month_start = Date(year, month_number, 1)

    if month_number == 12:
        month_end = Date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = Date(year, month_number + 1, 1) - timedelta(days=1)

    employee_filter: dict[str, Any] = {
    "joined_on": {"$lte": month_end.isoformat()}
}

    if department is not None:
        employee_filter["department"] = department

    pipeline = [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "date": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"]
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    2
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    6
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1
                                        ]
                                    },
                                    0
                                ]
                            }
                        }
                    }
                },
                "work_hours_values": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$$log.status",
                                                ["PRESENT", "WFH", "ON_DUTY"]
                                            ]
                                        },
                                        {"$ne": ["$$log.work_hours", None]}
                                    ]
                                }
                            }
                        },
                        "as": "log",
                        "in": "$$log.work_hours"
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0
                                ]
                            }
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]}
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_values": {"$push": "$work_hours_values"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "work_hours_values": {
                    "$reduce": {
                        "input": "$work_hours_values",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []

    for result in results:
        hours = result.pop("work_hours_values")
        avg_work_hours = None

        if hours:
            avg_work_hours = float(
                (
                    sum(Decimal(str(value)) for value in hours)
                    / Decimal(len(hours))
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

        result["avg_work_hours"] = avg_work_hours
        items.append(result)

    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, month_number = map(int, month.split("-"))

    if year < 1 or year > 9998:
        raise HTTPException(422, "Invalid month")

    month_start = Date(year, month_number, 1)

    if month_number == 12:
        month_end = Date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = Date(year, month_number + 1, 1) - timedelta(days=1)

    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat()
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    try:
        start_date = Date.fromisoformat(date_from.strip())
        end_date = Date.fromisoformat(date_to.strip())

        if start_date.isoformat() != date_from or end_date.isoformat() != date_to:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "Dates must be valid YYYY-MM-DD values")

    if end_date < start_date:
        raise HTTPException(422, "'to' must be on or after 'from'")

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")

    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": date_to},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", date_from]},
                                    {"$lte": ["$date", date_to]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$logs.status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": "$_id"
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$match": {"date": {"$ne": None}}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start_date, time.min),
                        datetime.combine(end_date + timedelta(days=1), time.min),
                    ],
                },
            }
        },
        {
            "$set": {
                "date_text": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date_text"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$trend_date"]},
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.headcount", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {"$divide": ["$present_count", "$headcount"]},
                                        100,
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_text",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = float(
                Decimal(str(item["attendance_rate"])).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )

        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = float(
                Decimal(str(item["moving_avg_7d"])).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )

    return {
        "department": department,
        "from": date_from,
        "to": date_to,
        "items": items,
    }

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50),
):
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in supported:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(supported)}",
        )

    today = Date.today()
    selected_month = month or today.strftime("%Y-%m")

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", selected_month):
        raise HTTPException(422, "month must be YYYY-MM")

    year, month_number = map(int, selected_month.split("-"))
    month_start = Date(year, month_number, 1)

    if month_number == 12:
        month_end = Date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = Date(year, month_number + 1, 1) - timedelta(days=1)

    collection = "attendance_logs"
    pipeline = None

    if endpoint == "attendance_list":
        query: dict[str, Any] = {}

        if emp_code:
            query["emp_code"] = emp_code

        if status:
            query["status"] = status

        if date_from or date_to:
            date_filter = {}

            if date_from:
                try:
                    Date.fromisoformat(date_from)
                except ValueError:
                    raise HTTPException(422, "'from' must be YYYY-MM-DD")
                date_filter["$gte"] = date_from

            if date_to:
                try:
                    Date.fromisoformat(date_to)
                except ValueError:
                    raise HTTPException(422, "'to' must be YYYY-MM-DD")
                date_filter["$lte"] = date_to

            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "'from' must be on or before 'to'")

            query["date"] = date_filter

        command = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

        result = db.command({
            "explain": command,
            "verbosity": "executionStats",
        })

    else:
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")

            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {
                            "$gte": month_start.isoformat(),
                            "$lte": month_end.isoformat(),
                        },
                    }
                },
                {"$sort": {"date": 1}},
            ]

        elif endpoint == "department_summary":
            collection = "employees"
            employee_filter: dict[str, Any] = {
                "joined_on": {"$lte": month_end.isoformat()}
            }

            if department:
                employee_filter["department"] = department

            pipeline = [
                {"$match": employee_filter},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"employee_code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$emp_code",
                                                    "$$employee_code",
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    "$date",
                                                    month_start.isoformat(),
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    "$date",
                                                    month_end.isoformat(),
                                                ]
                                            },
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },
            ]

        elif endpoint == "late_leaderboard":
            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": month_start.isoformat(),
                            "$lte": month_end.isoformat(),
                        }
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        },
                    }
                },
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
            ]

            if department:
                pipeline.append({
                    "$match": {"employee.department": department}
                })

            pipeline.extend([
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {"$limit": limit},
            ])

        elif endpoint == "department_trend":
            if not department:
                raise HTTPException(422, "department is required")

            start_text = date_from or month_start.isoformat()
            end_text = date_to or month_end.isoformat()

            try:
                start_date = Date.fromisoformat(start_text)
                end_date = Date.fromisoformat(end_text)
            except ValueError:
                raise HTTPException(422, "'from' and 'to' must be YYYY-MM-DD")

            if end_date < start_date:
                raise HTTPException(422, "'to' must be on or after 'from'")

            if (end_date - start_date).days + 1 > 92:
                raise HTTPException(422, "Date range cannot exceed 92 days")

            collection = "employees"
            pipeline = [
                {
                    "$match": {
                        "department": department,
                        "joined_on": {"$lte": end_text},
                    }
                },
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"employee_code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$emp_code",
                                                    "$$employee_code",
                                                ]
                                            },
                                            {"$gte": ["$date", start_text]},
                                            {"$lte": ["$date", end_text]},
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },
            ]

        result = db.command({
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

    return {
        "endpoint": endpoint,
        "execution_stats": result,
    }





# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# -----------------------------------------------------------------------------