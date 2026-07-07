"""
app/api/v1/endpoints/academic.py

Phase 1 upgrades:
  - All endpoints converted to async def with AsyncSession
  - db.query() → await db.execute(select(...))
  - Duplicate /all-timetables route removed (kept the admin-scoped one)
  - WebSocket added for QR token push (kills lecturer polling)
  - WebSocket added for live attendance feed
  - Assignment file uploads → Cloudinary (was local disk)
  - Course material uploads → Cloudinary (was local disk)
  - Missing auth added: session/start, session/stop, session/qr,
    assignments/bulk-grade, delete assignment, manual attendance,
    eligibility overrides, roster fetch, materials, announcements
  - Removed duplicate get_db() defined locally (was also in deps.py)
  - print() → logger throughout
"""
import logging
import math
import secrets
import uuid
from datetime import datetime, time, date, timedelta, timezone
from typing import List, Optional

import cloudinary.uploader
from fastapi import (
    APIRouter, Depends, HTTPException, status,
    WebSocket, WebSocketDisconnect,
    UploadFile, File, Form, Body,
)
from pydantic import BaseModel
from sqlalchemy import select, text, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_admin, get_current_user
from app.api.logger import log_to_db
from app.models.academic import (
    Department, Degree, SessionBatch, Semester, Section,
    Subject, SubjectOffering, Classroom, Timetable, ClassSession,
    CourseMaterial, Announcement,
)
from app.models.attendance import Avatar, AvatarMoodLog
from app.models.performance import (
    SessionalMark, AttendanceLog, Assessment, StudentAssessmentRecord,
)
from app.models.system import Notification
from app.models.users import (
    User, UserRole, Student, UserDevice, DeviceStatus, Lecturer, Admin,
)

logger = logging.getLogger("iqrat.academic")
router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# INPUT SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class DeptCreate(BaseModel):
    name: str
    code: str

class DegreeCreate(BaseModel):
    name: str
    code: str
    department_id: int

class BatchCreate(BaseModel):
    degree_id: int
    name: str
    start_year: int
    end_year: int

class SemesterCreate(BaseModel):
    session_id: int
    name: str
    semester_no: int

class SectionCreate(BaseModel):
    semester_id: int
    name: str

class SubjectCreate(BaseModel):
    degree_id: int
    semester_no: int
    name: str
    code: str
    credit_hours: int

class EnrollStudents(BaseModel):
    subject_id: int
    semester_id: int
    student_ids: List[int]

class ClassroomCreate(BaseModel):
    department_id: int
    room_no: str
    building_name: str
    latitude: float
    longitude: float
    capacity: int = 60

class OfferingCreate(BaseModel):
    subject_id: int
    semester_id: int
    lecturer_id: int

class TimetableCreate(BaseModel):
    offering_id: int
    classroom_id: int
    day_of_week: str
    start_time: str
    end_time: str

class TimetableUpdate(BaseModel):
    day_of_week: str
    start_time: str
    end_time: str
    classroom_id: int

class SessionStartRequest(BaseModel):
    timetable_id: int
    latitude: float
    longitude: float

class QRScanRequest(BaseModel):
    token: str
    latitude: float
    longitude: float
    device_fingerprint: str
    device_name: str

class GradeSyncPayload(BaseModel):
    assessment_id: int
    student_id: int
    marks: float

class ManualAssessmentCreate(BaseModel):
    offering_id: int
    title: str
    category: str
    max_marks: float
    weightage: float

class AlertPayload(BaseModel):
    student_id: int
    message: str

class ManualAttendancePayload(BaseModel):
    offering_id: int
    date: str
    attendance: List[dict]

class EligibilityPayload(BaseModel):
    offering_id: int
    action: str

class TransferLecturerReq(BaseModel):
    new_lecturer_id: int


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_distance(lat1, lon1, lat2, lon2) -> float:
    """Haversine formula — returns distance in metres between two GPS points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    return f"{round(size_bytes / math.pow(1024, i), 1)} {size_name[i]}"


# ══════════════════════════════════════════════════════════════════════════════
# ACADEMIC STRUCTURE — CREATE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/departments")
async def create_dept(
    dept: DeptCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    new_dept = Department(name=dept.name, code=dept.code)
    db.add(new_dept)
    await db.commit()
    await db.refresh(new_dept)
    return new_dept


@router.post("/degrees")
async def create_degree(
    deg: DegreeCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Department).where(Department.id == deg.department_id))
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Department not found.")
    new_deg = Degree(name=deg.name, code=deg.code, department_id=deg.department_id)
    db.add(new_deg)
    await db.commit()
    await db.refresh(new_deg)
    return new_deg


@router.post("/batches")
async def create_batch(
    batch: BatchCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    new_batch = SessionBatch(
        degree_id=batch.degree_id, name=batch.name,
        start_year=batch.start_year, end_year=batch.end_year,
    )
    db.add(new_batch)
    await db.commit()
    await db.refresh(new_batch)
    return new_batch


@router.post("/semesters")
async def create_semester(
    sem: SemesterCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    new_sem = Semester(session_id=sem.session_id, name=sem.name, semester_no=sem.semester_no)
    db.add(new_sem)
    await db.commit()
    await db.refresh(new_sem)
    return new_sem


@router.post("/sections")
async def create_section(
    sec: SectionCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    new_sec = Section(semester_id=sec.semester_id, name=sec.name)
    db.add(new_sec)
    await db.commit()
    await db.refresh(new_sec)
    return new_sec


@router.post("/subjects")
async def create_subject(
    sub: SubjectCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    new_sub = Subject(
        degree_id=sub.degree_id, semester_no=sub.semester_no,
        name=sub.name, code=sub.code, credit_hours=sub.credit_hours,
    )
    db.add(new_sub)
    await db.commit()
    await db.refresh(new_sub)
    return new_sub


# ══════════════════════════════════════════════════════════════════════════════
# DROPDOWN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/departments")
async def get_all_departments(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()

    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id = getattr(admin_profile, "department_id", None)

    if not is_super and dept_id:
        result = await db.execute(select(Department).where(Department.id == dept_id))
    else:
        result = await db.execute(select(Department))
    return result.scalars().all()


@router.get("/degrees/{department_id}")
async def get_degrees_by_dept(
    department_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Degree).where(Degree.department_id == department_id))
    return result.scalars().all()


@router.get("/batches/{degree_id}")
async def get_batches_by_degree(
    degree_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(SessionBatch).where(SessionBatch.degree_id == degree_id))
    return result.scalars().all()


@router.get("/semesters/{session_id}")
async def get_semesters_by_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Semester).where(Semester.session_id == session_id))
    return result.scalars().all()


@router.get("/sections/{semester_id}")
async def get_sections_by_semester(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Section).where(Section.semester_id == semester_id))
    return result.scalars().all()


@router.get("/subjects/{degree_id}")
async def get_subjects_by_degree(
    degree_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Subject).where(Subject.degree_id == degree_id))
    return result.scalars().all()


# ══════════════════════════════════════════════════════════════════════════════
# ENROLLMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/enroll-students")
async def enroll_students(
    data: EnrollStudents,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    count = 0
    for student_id in data.student_ids:
        result = await db.execute(
            select(SessionalMark).where(
                SessionalMark.student_id == student_id,
                SessionalMark.subject_id == data.subject_id,
                SessionalMark.semester_id == data.semester_id,
            )
        )
        if not result.scalars().first():
            db.add(SessionalMark(
                student_id=student_id,
                subject_id=data.subject_id,
                semester_id=data.semester_id,
                midterm_marks=0,
                total_sessional_marks=0,
            ))
            count += 1
    await db.commit()
    return {"msg": f"Successfully enrolled {count} students."}


# ══════════════════════════════════════════════════════════════════════════════
# CLASSROOMS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/classrooms")
async def create_classroom(
    room: ClassroomCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Classroom).where(Classroom.room_no == room.room_no))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Classroom already exists.")
    new_room = Classroom(**room.model_dump())
    db.add(new_room)
    await db.commit()
    await db.refresh(new_room)
    return new_room


@router.get("/classrooms")
async def get_classrooms(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id = getattr(admin_profile, "department_id", None)

    if not is_super and dept_id:
        result = await db.execute(select(Classroom).where(Classroom.department_id == dept_id))
    else:
        result = await db.execute(select(Classroom))
    return result.scalars().all()


# ══════════════════════════════════════════════════════════════════════════════
# SUBJECT OFFERINGS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/subject-offerings")
async def create_offering(
    data: OfferingCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(
        select(SubjectOffering).where(
            SubjectOffering.subject_id == data.subject_id,
            SubjectOffering.semester_id == data.semester_id,
            SubjectOffering.lecturer_id == data.lecturer_id,
        )
    )
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Lecturer already assigned to this subject.")
    new_offering = SubjectOffering(**data.model_dump(), is_active=True)
    db.add(new_offering)
    await db.commit()
    await db.refresh(new_offering)
    return {"msg": "Lecturer assigned successfully.", "id": new_offering.id}


@router.get("/subject-offerings/{semester_id}")
async def get_offerings_by_semester(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SubjectOffering).where(SubjectOffering.semester_id == semester_id)
    )
    offerings = result.scalars().all()
    return [{"offering_id": o.id, "subject": o.subject_id, "lecturer": o.lecturer_id} for o in offerings]


# ══════════════════════════════════════════════════════════════════════════════
# TIMETABLE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/timetables")
async def create_timetable_slot(
    slot: TimetableCreate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    try:
        t_start = time.fromisoformat(slot.start_time)
        t_end = time.fromisoformat(slot.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM:SS")

    new_slot = Timetable(
        offering_id=slot.offering_id,
        classroom_id=slot.classroom_id,
        day_of_week=slot.day_of_week,
        start_time=t_start,
        end_time=t_end,
    )
    db.add(new_slot)
    await db.commit()
    await db.refresh(new_slot)
    return {"msg": "Timetable slot created.", "id": new_slot.id}


@router.put("/timetables/{timetable_id}")
async def update_timetable_slot(
    timetable_id: int,
    data: TimetableUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Timetable).where(Timetable.id == timetable_id))
    slot = result.scalars().first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found.")
    try:
        slot.start_time = time.fromisoformat(data.start_time)
        slot.end_time = time.fromisoformat(data.end_time)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM:SS")
    slot.day_of_week = data.day_of_week
    slot.classroom_id = data.classroom_id
    await db.commit()
    return {"msg": "Timetable updated. Attendance history preserved."}


@router.delete("/timetables/{timetable_id}")
async def delete_timetable_slot(
    timetable_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Timetable).where(Timetable.id == timetable_id))
    slot = result.scalars().first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found.")

    await db.execute(text("DELETE FROM attendance_logs WHERE timetable_id = :tid"), {"tid": timetable_id})
    await db.execute(text("DELETE FROM class_sessions WHERE timetable_id = :tid"), {"tid": timetable_id})
    await db.delete(slot)
    await db.commit()
    return {"msg": "Timetable slot and associated records deleted."}


# ══════════════════════════════════════════════════════════════════════════════
# UNIVERSAL ADMIN FETCH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/all-subjects")
async def get_all_subjects_universal(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id = getattr(admin_profile, "department_id", None)

    if not is_super and dept_id:
        result = await db.execute(
            select(Subject).join(Degree).where(Degree.department_id == dept_id)
        )
    else:
        result = await db.execute(select(Subject))
    return result.scalars().all()


@router.get("/all-semesters")
async def get_all_semesters_universal(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id = getattr(admin_profile, "department_id", None)

    if not is_super and dept_id:
        result = await db.execute(
            select(Semester).join(SessionBatch).join(Degree).where(Degree.department_id == dept_id)
        )
    else:
        result = await db.execute(select(Semester))
    return result.scalars().all()


@router.get("/all-offerings")
async def get_all_offerings_universal(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id = getattr(admin_profile, "department_id", None)

    if not is_super and dept_id:
        result = await db.execute(
            select(SubjectOffering).join(Subject).join(Degree).where(Degree.department_id == dept_id)
        )
    else:
        result = await db.execute(select(SubjectOffering))
    return result.scalars().all()


@router.get("/all-timetables")
async def get_all_timetables_universal(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    """
    Single /all-timetables route — admin-scoped.
    🎓 The duplicate unauthenticated version has been removed. It was the source
    of the 'Duplicate Operation ID' FastAPI warning you saw on startup.
    """
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id = getattr(admin_profile, "department_id", None)

    if not is_super and dept_id:
        result = await db.execute(
            select(Timetable)
            .join(SubjectOffering)
            .join(Subject)
            .join(Degree)
            .where(Degree.department_id == dept_id)
        )
    else:
        result = await db.execute(select(Timetable))
    return result.scalars().all()


# ══════════════════════════════════════════════════════════════════════════════
# LIVE QR SESSIONS — REST ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/session/start")
async def start_session(
    req: SessionStartRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    new_session = ClassSession(
        timetable_id=req.timetable_id,
        session_date=date.today(),
        status="active",
        lecturer_latitude=req.latitude,
        lecturer_longitude=req.longitude,
    )
    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)
    logger.info("Session started: id=%d timetable=%d by user=%d",
                new_session.id, req.timetable_id, current_user.id)
    return {"msg": "Session started.", "session_id": new_session.id}


@router.get("/session/qr/{session_id}")
async def get_qr_token(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    """
    REST fallback for QR token — still works but the WebSocket endpoint
    below is the preferred path (no polling, server pushes every 15s).
    """
    result = await db.execute(select(ClassSession).where(ClassSession.id == session_id))
    session = result.scalars().first()
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Session not active.")

    new_token = secrets.token_urlsafe(32)
    session.current_qr_token = new_token
    session.qr_expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)
    await db.commit()
    return {"qr_token": new_token}


@router.post("/session/stop/{session_id}")
async def stop_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    result = await db.execute(select(ClassSession).where(ClassSession.id == session_id))
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.status == "active":
        session.status = "completed"
        session.current_qr_token = None
        session.qr_expires_at = None

        result = await db.execute(select(Timetable).where(Timetable.id == session.timetable_id))
        timetable = result.scalars().first()
        if timetable:
            result = await db.execute(
                select(SubjectOffering).where(SubjectOffering.id == timetable.offering_id)
            )
            offering = result.scalars().first()
            if offering:
                result = await db.execute(
                    select(SessionalMark).where(
                        SessionalMark.subject_id == offering.subject_id,
                        SessionalMark.semester_id == offering.semester_id,
                    )
                )
                enrollments = result.scalars().all()
                enrolled_ids = {e.student_id for e in enrollments}

                result = await db.execute(
                    select(AttendanceLog).where(
                        AttendanceLog.session_id == str(session.id)
                    )
                )
                present_ids = {log.student_id for log in result.scalars().all()}

                for student_id in enrolled_ids - present_ids:
                    db.add(AttendanceLog(
                        student_id=student_id,
                        timetable_id=session.timetable_id,
                        session_id=str(session.id),
                        status="Absent",
                    ))

        await db.commit()
        logger.info("Session %d stopped by user %d", session_id, current_user.id)
        return {"msg": "Session stopped. Absentees auto-recorded."}

    return {"msg": "Session was already closed."}


# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET — QR TOKEN PUSH
#
# 🎓 How this works:
#   Lecturer's frontend connects once: new WebSocket(ws://backend/ws/session/42/qr)
#   Server holds the connection open in a loop.
#   Every 15 seconds the server generates a new token, saves it to DB,
#   and pushes it directly to the lecturer's browser — no polling needed.
#   When the lecturer closes the page or stops the session, the connection closes
#   and the loop exits cleanly via WebSocketDisconnect.
#
# Frontend change (replace setInterval polling with):
#   const ws = new WebSocket(`${WS_URL}/academic/ws/session/${sessionId}/qr`);
#   ws.onmessage = (e) => { const { qr_token } = JSON.parse(e.data); setQrCode(qr_token); };
# ══════════════════════════════════════════════════════════════════════════════

import asyncio

@router.websocket("/ws/session/{session_id}/qr")
async def websocket_qr_push(
    websocket: WebSocket,
    session_id: int,
):
    """
    WebSocket endpoint that pushes a fresh QR token to the lecturer every 15 seconds.
    Replaces the polling pattern (GET /session/qr/{id} every 10s).

    🎓 Why we don't use Depends(get_db) here:
    A WebSocket connection stays open for up to 10 minutes. If we injected a single
    AsyncSession via Depends(), that session would hold a DB connection from the pool
    for the entire duration. With pool_size=10 and 30 concurrent lecturers, this
    exhausts the pool and blocks all other requests. Instead we open a fresh session
    per loop iteration (each one lasts ~milliseconds), releasing the connection
    back to the pool during the 15-second asyncio.sleep().
    """
    from app.db.session import AsyncSessionLocal  # local import avoids circular dep

    await websocket.accept()
    logger.info("WS QR connection opened for session %d", session_id)

    try:
        while True:
            # Open a short-lived session just for this iteration
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ClassSession).where(ClassSession.id == session_id)
                )
                session = result.scalars().first()

                if not session or session.status != "active":
                    await websocket.send_json({"error": "Session ended."})
                    return  # exits the while loop and the try block cleanly

                # Generate and persist new token
                new_token = secrets.token_urlsafe(32)
                session.current_qr_token = new_token
                session.qr_expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)
                await db.commit()

            # Push to lecturer's browser AFTER closing the DB session
            await websocket.send_json({
                "qr_token": new_token,
                "expires_in": 15,
            })

            # Sleep 15s — connection pool is fully free during this wait
            await asyncio.sleep(15)

    except WebSocketDisconnect:
        logger.info("WS QR connection closed for session %d", session_id)
    except Exception as e:
        logger.error("WS QR error for session %d: %s", session_id, e)
        await websocket.close()


@router.websocket("/ws/session/{session_id}/live")
async def websocket_live_attendance(
    websocket: WebSocket,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Pushes live attendance count to the lecturer every 3 seconds.
    Replaces the /session/{id}/live-roster polling endpoint.

    Frontend:
      const ws = new WebSocket(`${WS_URL}/academic/ws/session/${sessionId}/live`);
      ws.onmessage = (e) => { const { present_ids, count } = JSON.parse(e.data); ... };
    """
    await websocket.accept()
    logger.info("WS live attendance opened for session %d", session_id)

    try:
        while True:
            result = await db.execute(
                select(AttendanceLog).where(
                    AttendanceLog.session_id == str(session_id),
                    AttendanceLog.status == "Present",
                )
            )
            logs = result.scalars().all()
            present_ids = [log.student_id for log in logs]

            await websocket.send_json({
                "present_ids": present_ids,
                "count": len(present_ids),
            })

            await asyncio.sleep(3)

    except WebSocketDisconnect:
        logger.info("WS live attendance closed for session %d", session_id)
    except Exception as e:
        logger.error("WS live attendance error: %s", e)
        await websocket.close()


# ══════════════════════════════════════════════════════════════════════════════
# QR SCAN & GEOFENCING
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/session/scan")
async def validate_qr_scan(
    req: QRScanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Find session by token
    result = await db.execute(
        select(ClassSession).where(ClassSession.current_qr_token == req.token)
    )
    session = result.scalars().first()
    if not session or session.status != "active":
        raise HTTPException(status_code=400, detail="Invalid or expired QR code.")

    # 2. Check token expiry
    if datetime.now(timezone.utc) > session.qr_expires_at:
        raise HTTPException(status_code=400, detail="QR code expired. Please wait for the next one.")

    # 3. Geofence check
    if session.lecturer_latitude and session.lecturer_longitude:
        dist = calculate_distance(
            req.latitude, req.longitude,
            session.lecturer_latitude, session.lecturer_longitude,
        )
        if dist > 50.0:
            raise HTTPException(
                status_code=403,
                detail=f"Geofence failed. You are {int(dist)}m from the classroom (max 50m).",
            )

    # 4. Find student
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=403, detail="Only registered students can mark attendance.")

    # 5. Device fingerprint check
    result = await db.execute(
        select(UserDevice).where(
            UserDevice.user_id == current_user.id,
            UserDevice.status == DeviceStatus.ACTIVE,
        )
    )
    active_devices = result.scalars().all()

    if not active_devices:
        # First scan — lock this device
        db.add(UserDevice(
            user_id=current_user.id,
            device_fingerprint=req.device_fingerprint,
            device_name=req.device_name,
            status=DeviceStatus.ACTIVE,
        ))
        await db.commit()
        logger.info("First-time device locked for student %s", student.reg_no)
    else:
        known = [d.device_fingerprint for d in active_devices]
        if req.device_fingerprint not in known:
            raise HTTPException(
                status_code=403,
                detail="Unrecognized device. Submit a Device Change Request from your profile.",
            )

    # 6. Duplicate scan check
    result = await db.execute(
        select(AttendanceLog).where(
            AttendanceLog.session_id == str(session.id),
            AttendanceLog.student_id == student.id,
        )
    )
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Attendance already recorded for this session.")

    # 7. Log attendance
    new_log = AttendanceLog(
        student_id=student.id,
        timetable_id=session.timetable_id,
        session_id=str(session.id),
        status="Present",
        device_fingerprint=req.device_fingerprint,
    )
    db.add(new_log)
    await db.commit()
    await db.refresh(new_log)

    return {"msg": "Attendance marked successfully!", "status": "present", "log_id": new_log.id}


# ══════════════════════════════════════════════════════════════════════════════
# LECTURER — ROSTER & GRADING
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/offerings/{offering_id}/roster")
async def get_class_roster(
    offering_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Offering not found.")

    result = await db.execute(
        select(Assessment).where(
            Assessment.subject_id == offering.subject_id,
            Assessment.semester_id == offering.semester_id,
        )
    )
    assessments = result.scalars().all()
    assessment_ids = [a.id for a in assessments]

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == offering.subject_id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    enrollments = result.scalars().all()

    roster = []
    for enr in enrollments:
        result = await db.execute(select(Student).where(Student.id == enr.student_id))
        student = result.scalars().first()
        if not student:
            continue

        result = await db.execute(
            select(StudentAssessmentRecord).where(
                StudentAssessmentRecord.student_id == student.id,
                StudentAssessmentRecord.assessment_id.in_(assessment_ids),
            )
        )
        records = result.scalars().all()
        marks_dict = {r.assessment_id: r.obtained_marks for r in records if r.obtained_marks is not None}
        subs_dict  = {r.assessment_id: r.submitted_file_path for r in records if r.submitted_file_path}

        roster.append({
            "id":           student.id,
            "name":         student.full_name,
            "roll":         student.reg_no,
            "status":       "absent",
            "attendancePct": 100,
            "avgGrade":     0,
            "marks":        marks_dict,
            "submissions":  subs_dict,
        })

    return roster


@router.get("/session/{session_id}/live-roster")
async def get_live_session_roster(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """REST fallback for live roster. WebSocket /ws/session/{id}/live is preferred."""
    result = await db.execute(
        select(AttendanceLog).where(
            AttendanceLog.session_id == str(session_id),
            AttendanceLog.status == "Present",
        )
    )
    logs = result.scalars().all()
    return {"present_ids": [log.student_id for log in logs]}


# ══════════════════════════════════════════════════════════════════════════════
# ASSIGNMENTS — Cloudinary upload (was local disk)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/assignments")
async def create_assignment(
    offering_id: int = Form(...),
    title: str = Form(...),
    deadline: str = Form(...),
    max_marks: float = Form(...),
    weightage: float = Form(...),
    description: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
    lecturer = result.scalars().first()
    if not lecturer:
        raise HTTPException(status_code=403, detail="Only lecturers can create assignments.")

    result = await db.execute(
        select(SubjectOffering).where(
            SubjectOffering.id == offering_id,
            SubjectOffering.lecturer_id == lecturer.id,
        )
    )
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Course offering not found or unauthorized.")

    # Upload question file to Cloudinary (was static/assignments/ local disk)
    file_url = None
    if file:
        try:
            upload_result = cloudinary.uploader.upload(
                file.file,
                folder="iqrat_assignments",
                public_id=f"assignment_{offering_id}_{uuid.uuid4().hex[:8]}",
                resource_type="raw",
            )
            file_url = upload_result.get("secure_url")
        except Exception as e:
            logger.error("Cloudinary assignment upload failed: %s", e)
            raise HTTPException(status_code=500, detail="Failed to upload assignment file.")

    try:
        deadline_date = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        deadline_date = None

    new_assessment = Assessment(
        subject_id=offering.subject_id,
        semester_id=offering.semester_id,
        name=title,
        category="Assignment",
        max_marks=max_marks,
        weightage=weightage,
        description=description,
        deadline=deadline_date,
        file_path=file_url,
        status="Active",
    )
    db.add(new_assessment)
    await db.flush()

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == offering.subject_id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    enrollments = result.scalars().all()

    result = await db.execute(select(Subject).where(Subject.id == offering.subject_id))
    subject = result.scalars().first()

    for enr in enrollments:
        result = await db.execute(select(Student).where(Student.id == enr.student_id))
        student = result.scalars().first()
        if student:
            db.add(StudentAssessmentRecord(
                assessment_id=new_assessment.id,
                student_id=student.id,
                status="Pending",
            ))
            db.add(Notification(
                user_id=student.user_id,
                title=f"New Assignment: {title}",
                message=f"Due {deadline} for {subject.name if subject else 'your class'}.",
                is_read=False,
                type="in_app",
            ))

    await db.commit()
    log_to_db(db=db, user_id=current_user.id, action="Created Assignment",
              entity_type="Assessment", entity_id=new_assessment.id, new_value=title)

    return {"msg": f"Assignment created. {len(enrollments)} students notified.", "assessment_id": new_assessment.id}


@router.get("/offerings/{offering_id}/assignments")
async def get_offering_assignments(
    offering_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        return []

    result = await db.execute(
        select(Assessment).where(
            Assessment.subject_id == offering.subject_id,
            Assessment.semester_id == offering.semester_id,
        )
    )
    assessments = result.scalars().all()

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == offering.subject_id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    total_students = len(result.scalars().all())

    output = []
    for ass in assessments:
        result = await db.execute(
            select(StudentAssessmentRecord).where(
                StudentAssessmentRecord.assessment_id == ass.id,
                StudentAssessmentRecord.status.in_(["Submitted", "Graded"]),
            )
        )
        sub_count = len(result.scalars().all())
        output.append({
            "id":          ass.id,
            "title":       ass.name,
            "deadline":    ass.deadline.strftime("%Y-%m-%d") if ass.deadline else "No Deadline",
            "submissions": sub_count,
            "total":       total_students,
            "status":      ass.status,
            "maxMarks":    ass.max_marks,
            "weight":      ass.weightage,
            "type":        ass.category,
        })
    return output


@router.put("/assignments/bulk-grade")
async def bulk_grade_assignments(
    grades: List[GradeSyncPayload],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    for g in grades:
        result = await db.execute(
            select(StudentAssessmentRecord).where(
                StudentAssessmentRecord.assessment_id == g.assessment_id,
                StudentAssessmentRecord.student_id == g.student_id,
            )
        )
        record = result.scalars().first()
        if record:
            record.obtained_marks = g.marks
            if record.status in ("Pending", "Submitted"):
                record.status = "Graded"
        else:
            db.add(StudentAssessmentRecord(
                assessment_id=g.assessment_id,
                student_id=g.student_id,
                obtained_marks=g.marks,
                status="Graded",
            ))
    await db.commit()
    return {"msg": "Grades synced successfully."}


@router.delete("/assignments/{assessment_id}")
async def delete_assignment(
    assessment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    result = await db.execute(select(Assessment).where(Assessment.id == assessment_id))
    ass = result.scalars().first()
    if not ass:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    await db.execute(
        delete(StudentAssessmentRecord).where(StudentAssessmentRecord.assessment_id == assessment_id)
    )
    await db.delete(ass)
    await db.commit()
    return {"msg": "Assignment deleted."}


@router.post("/assessments/manual")
async def create_manual_assessment(
    data: ManualAssessmentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
    lecturer = result.scalars().first()
    if not lecturer:
        raise HTTPException(status_code=403, detail="Only lecturers can create assessments.")

    result = await db.execute(
        select(SubjectOffering).where(
            SubjectOffering.id == data.offering_id,
            SubjectOffering.lecturer_id == lecturer.id,
        )
    )
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Offering not found.")

    new_assessment = Assessment(
        subject_id=offering.subject_id,
        semester_id=offering.semester_id,
        name=data.title,
        category=data.category,
        max_marks=data.max_marks,
        weightage=data.weightage,
        status="Active",
    )
    db.add(new_assessment)
    await db.flush()

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == offering.subject_id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    for enr in result.scalars().all():
        db.add(StudentAssessmentRecord(
            assessment_id=new_assessment.id,
            student_id=enr.student_id,
            status="Pending",
        ))

    await db.commit()
    return {"msg": "Manual column created.", "assessment_id": new_assessment.id}


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/students/alert")
async def send_student_alert(
    payload: AlertPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    result = await db.execute(select(Student).where(Student.id == payload.student_id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found.")
    db.add(Notification(
        user_id=student.user_id,
        title="⚠️ Academic Risk Alert",
        message=payload.message,
        is_read=False,
        type="in_app",
    ))
    await db.commit()
    return {"msg": "Alert sent."}


# ══════════════════════════════════════════════════════════════════════════════
# COURSE MATERIALS — Cloudinary (was local disk)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/offerings/{offering_id}/materials")
async def upload_material(
    offering_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    """
    Uploads course material to Cloudinary.
    🎓 resource_type="raw" = any file type (PDF, PPTX, DOCX, ZIP).
       resource_type="auto" would try to detect image/video and may reject PDFs.
    """
    file_content = await file.read()
    file_size = len(file_content)

    # Reset file pointer for upload
    import io
    file_bytes = io.BytesIO(file_content)

    try:
        upload_result = cloudinary.uploader.upload(
            file_bytes,
            folder="iqrat_materials",
            public_id=f"material_{offering_id}_{uuid.uuid4().hex[:8]}",
            resource_type="raw",
        )
        file_url = upload_result.get("secure_url")
    except Exception as e:
        logger.error("Cloudinary material upload failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to upload material.")

    new_material = CourseMaterial(
        offering_id=offering_id,
        title=file.filename,
        file_path=file_url,
        file_size=format_size(file_size),
    )
    db.add(new_material)
    await db.commit()
    await db.refresh(new_material)
    return new_material


@router.get("/offerings/{offering_id}/materials")
async def get_materials(
    offering_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(CourseMaterial)
        .where(CourseMaterial.offering_id == offering_id)
        .order_by(CourseMaterial.uploaded_at.desc())
    )
    items = result.scalars().all()
    return [
        {"id": i.id, "name": i.title, "path": i.file_path,
         "size": i.file_size, "date": i.uploaded_at.strftime("%b %d, %Y")}
        for i in items
    ]


@router.delete("/materials/{material_id}")
async def delete_material(
    material_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    await db.execute(delete(CourseMaterial).where(CourseMaterial.id == material_id))
    await db.commit()
    return {"msg": "Material deleted."}


# ══════════════════════════════════════════════════════════════════════════════
# ANNOUNCEMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/offerings/{offering_id}/announcements")
async def create_announcement(
    offering_id: int,
    title: str = Form(...),
    message: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    db.add(Announcement(offering_id=offering_id, title=title, message=message))

    # Get subject_id for enrollment lookup
    result = await db.execute(
        select(SubjectOffering.subject_id).where(SubjectOffering.id == offering_id)
    )
    subject_id = result.scalars().first()

    if subject_id:
        result = await db.execute(
            select(SessionalMark).where(SessionalMark.subject_id == subject_id)
        )
        for enr in result.scalars().all():
            result2 = await db.execute(select(Student).where(Student.id == enr.student_id))
            student = result2.scalars().first()
            if student:
                db.add(Notification(
                    user_id=student.user_id,
                    title=f"Announcement: {title}",
                    message=message,
                    type="in_app",
                ))

    await db.commit()
    return {"msg": "Announcement broadcasted successfully."}


@router.get("/offerings/{offering_id}/announcements")
async def get_announcements(
    offering_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Announcement)
        .where(Announcement.offering_id == offering_id)
        .order_by(Announcement.created_at.desc())
    )
    items = result.scalars().all()
    return [
        {"id": i.id, "title": i.title, "message": i.message,
         "date": i.created_at.strftime("%b %d, %Y")}
        for i in items
    ]


# ══════════════════════════════════════════════════════════════════════════════
# SUBJECT & OFFERING MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.delete("/subjects/{subject_id}")
async def delete_subject(
    subject_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(
        select(SubjectOffering).where(SubjectOffering.subject_id == subject_id)
    )
    if result.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Cannot delete — subject is assigned to a lecturer. Transfer or remove the offering first.",
        )
    await db.execute(delete(Subject).where(Subject.id == subject_id))
    await db.commit()
    return {"msg": "Subject deleted."}


@router.put("/subject-offerings/{offering_id}/transfer")
async def transfer_lecturer(
    offering_id: int,
    req: TransferLecturerReq,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Offering not found.")
    offering.lecturer_id = req.new_lecturer_id
    await db.commit()
    return {"msg": "Class transferred to new lecturer."}


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL ATTENDANCE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/session/manual-attendance")
async def save_manual_attendance(
    payload: ManualAttendancePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    try:
        manual_date = datetime.strptime(payload.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    result = await db.execute(
        select(Timetable).where(Timetable.offering_id == payload.offering_id)
    )
    timetable = result.scalars().first()
    if not timetable:
        raise HTTPException(status_code=400, detail="No timetable for this class.")

    result = await db.execute(
        select(ClassSession).where(
            ClassSession.timetable_id == timetable.id,
            ClassSession.session_date == manual_date,
        )
    )
    session = result.scalars().first()
    if not session:
        session = ClassSession(
            timetable_id=timetable.id,
            session_date=manual_date,
            status="completed",
        )
        db.add(session)
        await db.flush()

    for record in payload.attendance:
        student_id = record.get("student_id")
        status_text = record.get("status", "absent").capitalize()

        result = await db.execute(
            select(AttendanceLog).where(
                AttendanceLog.student_id == student_id,
                AttendanceLog.session_id == str(session.id),
            )
        )
        existing = result.scalars().first()
        if existing:
            existing.status = status_text
        else:
            db.add(AttendanceLog(
                student_id=student_id,
                timetable_id=timetable.id,
                session_id=str(session.id),
                status=status_text,
            ))

    await db.commit()
    return {"msg": "Manual attendance saved."}


@router.get("/offerings/{offering_id}/attendance/{date_str}")
async def get_historical_attendance(
    offering_id: int,
    date_str: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    result = await db.execute(
        select(Timetable).where(Timetable.offering_id == offering_id)
    )
    timetable = result.scalars().first()
    if not timetable:
        raise HTTPException(status_code=400, detail="No timetable for this class.")

    result = await db.execute(
        select(ClassSession).where(
            ClassSession.timetable_id == timetable.id,
            ClassSession.session_date == target_date,
        )
    )
    session = result.scalars().first()

    # Get offering details for enrollment lookup
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        return []

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == offering.subject_id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    enrollments = result.scalars().all()

    roster_data = []
    for enr in enrollments:
        result = await db.execute(select(Student).where(Student.id == enr.student_id))
        student = result.scalars().first()
        if not student:
            continue

        att_status = "absent"
        if session:
            result = await db.execute(
                select(AttendanceLog).where(
                    AttendanceLog.student_id == student.id,
                    AttendanceLog.session_id == str(session.id),
                )
            )
            log = result.scalars().first()
            if log:
                att_status = log.status.lower()

        roster_data.append({
            "id":   student.id,
            "name": student.full_name,
            "roll": student.reg_no,
            "status": att_status,
        })

    return roster_data


# ══════════════════════════════════════════════════════════════════════════════
# ELIGIBILITY OVERRIDES
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/students/{student_id}/override-eligibility")
async def override_student_eligibility(
    student_id: int,
    payload: EligibilityPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    result = await db.execute(
        select(Timetable).where(Timetable.offering_id == payload.offering_id)
    )
    timetable = result.scalars().first()
    if not timetable:
        raise HTTPException(status_code=400, detail="No timetable for this class.")

    target_status = "Present" if payload.action == "eligible" else "Absent"
    for i in range(5):
        fake_session = ClassSession(
            timetable_id=timetable.id,
            session_date=date.today() - timedelta(days=i),
            status="completed",
        )
        db.add(fake_session)
        await db.flush()
        db.add(AttendanceLog(
            student_id=student_id,
            timetable_id=timetable.id,
            session_id=str(fake_session.id),
            status=target_status,
        ))

    await db.commit()
    return {"msg": f"Student marked as {payload.action}."}


@router.post("/offerings/{offering_id}/override-all-eligible")
async def override_all_eligible(
    offering_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),   # was unauthenticated
):
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Offering not found.")

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == offering.subject_id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    enrollments = result.scalars().all()

    result = await db.execute(
        select(Timetable).where(Timetable.offering_id == offering_id)
    )
    timetable = result.scalars().first()
    if not timetable:
        raise HTTPException(status_code=400, detail="No timetable for this class.")

    for i in range(5):
        fake_session = ClassSession(
            timetable_id=timetable.id,
            session_date=date.today() - timedelta(days=i),
            status="completed",
        )
        db.add(fake_session)
        await db.flush()
        for enr in enrollments:
            db.add(AttendanceLog(
                student_id=enr.student_id,
                timetable_id=timetable.id,
                session_id=str(fake_session.id),
                status="Present",
            ))

    await db.commit()
    return {"msg": "All students marked eligible."}