"""
app/api/v1/endpoints/system.py

Phase 1 upgrades:
  - All endpoints converted to async def with AsyncSession
  - db.query() → await db.execute(select(...))
  - Duplicate Notification import removed
  - db.bulk_save_objects() → individual db.add() (async-safe)
  - Attendance (dead table) replaced with AttendanceLog for beacon student count
  - Auth added to: geofence/settings (POST+GET), geofence/violations,
    settings/academic (POST+GET), reports/history, reports/export,
    communication/history
  - Scoped admin logic kept intact, ported to async
"""
import csv
import logging
from datetime import datetime, timezone
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_admin, get_current_user
from app.models.academic import (
    SubjectOffering, Subject, Degree, ClassSession, Timetable, Classroom,
)
from app.models.attendance import ExceptionLog
from app.models.performance import SessionalMark, AttendanceLog
from app.models.system import SysLog, Setting, Notification
from app.models.users import Admin, Lecturer, Student, User

logger = logging.getLogger("iqrat.system")
router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _time_ago(ts: datetime | None) -> str:
    """Returns a human-readable 'X mins ago' string from a UTC datetime."""
    if not ts:
        return "Recently"
    diff = datetime.now(timezone.utc) - ts
    mins = int(diff.total_seconds() / 60)
    if mins < 60:
        return f"{mins} mins ago"
    elif mins < 1440:
        return f"{int(mins / 60)} hours ago"
    return f"{int(mins / 1440)} days ago"


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD STATS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard-stats")
async def get_admin_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    # Real alerts from SysLog
    result = await db.execute(
        select(SysLog)
        .where(SysLog.severity.in_(["critical", "warning"]))
        .order_by(SysLog.timestamp.desc())
        .limit(5)
    )
    recent_logs = result.scalars().all()

    alerts = [
        {
            "id":   log.id,
            "type": log.severity,
            "msg":  log.action,
            "time": log.timestamp.strftime("%b %d, %I:%M %p") if log.timestamp else "Just now",
        }
        for log in recent_logs
    ] or [
        {
            "id":   "healthy_01",
            "type": "nominal",
            "msg":  "All systems operating normally. No security breaches detected.",
            "time": "Just now",
        }
    ]

    # Admin scoping
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    is_super = "super" in str(getattr(admin_profile, "role_level", "")).lower()
    dept_id  = getattr(admin_profile, "department_id", None)

    # Active courses count
    q = select(func.count()).select_from(SubjectOffering).join(Subject).join(Degree).where(
        SubjectOffering.is_active == True
    )
    if not is_super and dept_id:
        q = q.where(Degree.department_id == dept_id)
    result = await db.execute(q)
    active_courses = result.scalar()

    return {
        "alerts": alerts,
        "stats": {
            "activeCourses": active_courses,
            "systemHealth":  "99.9% Uptime",
            "storageUsed":   "Stable",
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# GEOFENCING
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/geofence/active-beacons")
async def get_active_beacons(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    """Live radar — all currently active QR sessions with GPS coordinates."""
    result = await db.execute(
        select(ClassSession).where(ClassSession.status == "active")
    )
    active_sessions = result.scalars().all()

    results = []
    for session in active_sessions:
        if not session.lecturer_latitude or not session.lecturer_longitude:
            continue

        result = await db.execute(select(Timetable).where(Timetable.id == session.timetable_id))
        tt = result.scalars().first()
        if not tt:
            continue

        result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == tt.offering_id))
        offering = result.scalars().first()
        if not offering:
            continue

        result = await db.execute(select(Subject).where(Subject.id == offering.subject_id))
        subject = result.scalars().first()

        result = await db.execute(select(Lecturer).where(Lecturer.id == offering.lecturer_id))
        lecturer = result.scalars().first()

        result = await db.execute(select(Classroom).where(Classroom.id == tt.classroom_id))
        classroom = result.scalars().first()

        #    Using AttendanceLog (active table) instead of the dead `Attendance` model.
        #    The old Attendance table was never written to — all QR scans go to AttendanceLog.
        result = await db.execute(
            select(func.count()).select_from(AttendanceLog).where(
                AttendanceLog.session_id == str(session.id),
                AttendanceLog.status == "Present",
            )
        )
        present_count = result.scalar()

        results.append({
            "id":            session.id,
            "lecturer_name": lecturer.full_name if lecturer else "Unknown",
            "subject_name":  subject.name if subject else "Unknown",
            "lat":           str(session.lecturer_latitude),
            "lng":           str(session.lecturer_longitude),
            "loc":           classroom.room_no if classroom else "TBD",
            "students":      present_count,
        })

    return results


@router.get("/geofence/violations")
async def get_geofence_violations(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    result = await db.execute(
        select(ExceptionLog).order_by(ExceptionLog.id.desc()).limit(20)
    )
    logs = result.scalars().all()

    results = []
    for log in logs:
        result = await db.execute(select(User).where(User.id == log.raised_by))
        user = result.scalars().first()
        if not user:
            continue

        result = await db.execute(select(Student).where(Student.user_id == log.raised_by))
        student = result.scalars().first()
        if not student:
            continue

        results.append({
            "id":            log.id,
            "student_name":  student.full_name,
            "roll_no":       student.reg_no,
            "distance_away": log.reason or "Out of Bounds",
            "action_taken":  "Blocked" if log.resolution_status == "pending" else "Flagged",
            "time_ago":      _time_ago(getattr(log, "timestamp", None)),
        })

    return results


@router.get("/geofence/settings")
async def get_geofence_settings(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    result = await db.execute(select(Setting).where(Setting.key_name == "geofence_radius"))
    radius_setting = result.scalars().first()

    result = await db.execute(select(Setting).where(Setting.key_name == "geofence_strict_mode"))
    strict_setting = result.scalars().first()

    return {
        "allowed_radius": int(radius_setting.value) if radius_setting else 20,
        "strict_mode":    strict_setting.value.lower() == "true" if strict_setting else True,
    }


@router.post("/geofence/settings")
async def update_geofence_settings(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    """
    🎓 Upsert pattern — update if exists, insert if not.
    In async SQLAlchemy we can't use a helper that does db.query() inside it,
    so we inline the logic with await.
    """
    # Validate radius — must be positive
    radius = data.get("allowed_radius", 20)
    if not isinstance(radius, (int, float)) or radius <= 0:
        raise HTTPException(status_code=400, detail="Geofence radius must be a positive number.")

    for key, val in [
        ("geofence_radius", str(radius)),
        ("geofence_strict_mode", "true" if data.get("strict_mode", True) else "false"),
    ]:
        result = await db.execute(select(Setting).where(Setting.key_name == key))
        setting = result.scalars().first()
        if setting:
            setting.value = val
        else:
            db.add(Setting(key_name=key, value=val, category="security"))

    await db.commit()
    return {"msg": "Geofence settings updated."}


# ══════════════════════════════════════════════════════════════════════════════
# COMMUNICATION CENTER
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/communication/broadcast")
async def send_broadcast(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    target_type = data.get("target")
    specific_id  = data.get("specificId")
    title        = data.get("title", "").strip()
    message      = data.get("body", "").strip()

    if not title or not message:
        raise HTTPException(status_code=400, detail="Title and message are required.")

    # ── Gather target users ──────────────────────────────────────────────────
    users_to_notify: list[User] = []

    if target_type == "all":
        result = await db.execute(select(User).where(User.is_active == True))
        users_to_notify = result.scalars().all()

    elif target_type == "students":
        result = await db.execute(
            select(User).where(User.role == "student", User.is_active == True)
        )
        users_to_notify = result.scalars().all()

    elif target_type == "lecturers":
        result = await db.execute(
            select(User).where(User.role == "lecturer", User.is_active == True)
        )
        users_to_notify = result.scalars().all()

    elif target_type == "dept" and specific_id:
        dept_id = int(specific_id)
        result = await db.execute(
            select(User).join(Student).join(Degree).where(Degree.department_id == dept_id)
        )
        dept_students = result.scalars().all()
        result = await db.execute(
            select(User).join(Lecturer).where(Lecturer.department_id == dept_id)
        )
        dept_lecturers = result.scalars().all()
        users_to_notify = dept_students + dept_lecturers

    elif target_type == "specific" and specific_id:
        result = await db.execute(
            select(Student).where(Student.reg_no == specific_id)
        )
        student = result.scalars().first()
        if student:
            result = await db.execute(select(User).where(User.id == student.user_id))
            u = result.scalars().first()
            if u:
                users_to_notify = [u]
        if not users_to_notify:
            result = await db.execute(
                select(Lecturer).where(Lecturer.employee_code == specific_id)
            )
            lecturer = result.scalars().first()
            if lecturer:
                result = await db.execute(select(User).where(User.id == lecturer.user_id))
                u = result.scalars().first()
                if u:
                    users_to_notify = [u]

    if not users_to_notify:
        raise HTTPException(status_code=404, detail="No users found for the selected target.")

    # ── Create notifications ─────────────────────────────────────────────────
    #    db.bulk_save_objects() is sync-only — it bypasses the async session.
    #    In async SQLAlchemy we add objects individually; the session batches
    #    them into a single INSERT automatically at flush/commit time.
    seen_ids: set[int] = set()
    count = 0
    for u in users_to_notify:
        if u and u.id not in seen_ids:
            seen_ids.add(u.id)
            db.add(Notification(
                user_id=u.id,
                title=title,
                message=message,
                type="in_app",
            ))
            count += 1

    # ── Audit log ────────────────────────────────────────────────────────────
    target_label = (
        target_type.upper() if target_type != "specific" else specific_id
    )
    if target_type == "dept":
        target_label = f"DEPT_{specific_id}"

    db.add(SysLog(
        user_id=current_admin.id,
        action=f"BROADCAST|{title}|{message}|{target_label}|{count}",
        module="COMMUNICATION",
        severity="info",
    ))

    await db.commit()
    logger.info("Broadcast sent to %d users by admin %d", count, current_admin.id)
    return {"msg": f"Broadcast delivered to {count} users."}


@router.get("/communication/history")
async def get_broadcast_history(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    result = await db.execute(
        select(SysLog)
        .where(SysLog.module == "COMMUNICATION", SysLog.action.like("BROADCAST|%"))
        .order_by(SysLog.id.desc())
        .limit(20)
    )
    logs = result.scalars().all()

    history = []
    for log in logs:
        parts = log.action.split("|")
        if len(parts) < 5:
            continue
        target_clean = (
            parts[3]
            .replace("ALL", "All Users")
            .replace("STUDENTS", "All Students")
            .replace("LECTURERS", "All Lecturers")
        )
        if target_clean.startswith("DEPT_"):
            target_clean = f"Department ID: {target_clean.split('_')[1]}"

        history.append({
            "id":     log.id,
            "title":  parts[1],
            "body":   parts[2],
            "target": target_clean,
            "count":  parts[4],
            "date":   _time_ago(log.timestamp),
        })

    return history


# ══════════════════════════════════════════════════════════════════════════════
# ACADEMIC SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/settings/academic")
async def get_academic_settings(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    async def get_val(key: str, default: str) -> str:
        result = await db.execute(select(Setting).where(Setting.key_name == key))
        s = result.scalars().first()
        return s.value if s else default

    return {
        "min_attendance_pct":   int(await get_val("min_attendance_pct", "80")),
        "semester_start_date":  await get_val("semester_start_date", ""),
        "semester_end_date":    await get_val("semester_end_date", ""),
        "grade_freeze_active":  (await get_val("grade_freeze_active", "false")).lower() == "true",
    }


@router.post("/settings/academic")
async def update_academic_settings(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    for key, val in [
        ("min_attendance_pct",  str(data.get("min_attendance_pct", 80))),
        ("semester_start_date", str(data.get("semester_start_date", ""))),
        ("semester_end_date",   str(data.get("semester_end_date", ""))),
        ("grade_freeze_active", "true" if data.get("grade_freeze_active") else "false"),
    ]:
        result = await db.execute(select(Setting).where(Setting.key_name == key))
        setting = result.scalars().first()
        if setting:
            setting.value = val
        else:
            db.add(Setting(key_name=key, value=val, category="academic"))

    await db.commit()
    return {"msg": "Academic policies updated."}


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/reports/submit-to-admin")
async def submit_report_to_admin(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    offering_id = data.get("offering_id")
    report_type = data.get("report_type")

    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Course not found.")

    result = await db.execute(select(Subject).where(Subject.id == offering.subject_id))
    subject = result.scalars().first()

    result = await db.execute(select(Lecturer).where(Lecturer.id == offering.lecturer_id))
    lecturer = result.scalars().first()

    db.add(SysLog(
        user_id=current_user.id,
        action=f"REPORT_SUBMITTED|{report_type}|{offering_id}|{subject.name}|{lecturer.full_name}",
        module="REPORTS",
        severity="info",
    ))

    result = await db.execute(select(User).where(User.role == "admin"))
    admin_users = result.scalars().all()
    for admin in admin_users:
        db.add(Notification(
            user_id=admin.id,
            title=f"New {report_type.capitalize()} Report Submitted",
            message=f"{lecturer.full_name} submitted the {report_type} report for {subject.name}.",
            type="in_app",
        ))

    await db.commit()
    return {"msg": f"{report_type.capitalize()} report submitted to administration."}


@router.get("/reports/history")
async def get_submitted_reports(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    result = await db.execute(
        select(SysLog)
        .where(SysLog.action.like("REPORT_SUBMITTED%"))
        .order_by(SysLog.id.desc())
    )
    logs = result.scalars().all()

    reports = []
    for log in logs:
        parts = log.action.split("|")
        if len(parts) < 5:
            continue
        reports.append({
            "id":          log.id,
            "type":        parts[1],
            "offering_id": parts[2],
            "subject":     parts[3],
            "lecturer":    parts[4],
            "date":        log.timestamp.strftime("%b %d, %Y") if log.timestamp else "Recently",
        })

    return reports


@router.get("/reports/export/grades/{offering_id}")
async def export_grades_csv(
    offering_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    """
    Streams a CSV of student grades for the given offering.
    🎓 StreamingResponse + StringIO lets us build the CSV in memory and stream
    it to the browser without saving anything to disk — stateless and fast.
    """
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id == offering_id))
    offering = result.scalars().first()
    if not offering:
        raise HTTPException(status_code=404, detail="Course not found.")

    result = await db.execute(select(Subject).where(Subject.id == offering.subject_id))
    subject = result.scalars().first()

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.subject_id == subject.id,
            SessionalMark.semester_id == offering.semester_id,
        )
    )
    enrollments = result.scalars().all()

    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Student ID", "Student Name", "Course Code", "Course Name", "Total Score", "Status"])

    for enr in enrollments:
        result = await db.execute(select(Student).where(Student.id == enr.student_id))
        student = result.scalars().first()
        if student:
            total  = enr.total_sessional_marks or 0
            status = "PASS" if total >= 50 else "FAIL"
            writer.writerow([
                student.reg_no, student.full_name,
                subject.code, subject.name,
                total, status,
            ])

    stream.seek(0)
    safe_name = subject.name.replace(" ", "_")
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=Grades_{safe_name}.csv"},
    )


@router.get("/reports/stats")
async def get_global_report_stats(
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),  # was unauthenticated
):
    result = await db.execute(select(func.count()).select_from(SessionalMark))
    total_enrollments = result.scalar()

    result = await db.execute(
        select(func.count()).select_from(SessionalMark).where(
            SessionalMark.total_sessional_marks >= 50
        )
    )
    passed_enrollments = result.scalar()

    pass_rate = (
        round((passed_enrollments / total_enrollments) * 100, 1)
        if total_enrollments > 0
        else 100.0
    )

    result = await db.execute(
        select(func.count(distinct(SessionalMark.student_id))).where(
            SessionalMark.total_sessional_marks < 50
        )
    )
    at_risk_count = result.scalar()

    return {
        "avg_pass_rate": pass_rate,
        "at_risk_count": at_risk_count,
    }