"""
app/api/v1/endpoints/users.py

Phase 0 fixes applied:
  - ALL /me/* endpoints now use Depends(get_current_user) — email query param auth bypass removed
  - Removed plaintext password print statements (users.py:95)
  - Assignment submissions migrated from local disk → Cloudinary
  - get_db() removed (was duplicated) — imported from deps.py only
  - from ast import List → replaced with correct typing imports
  - Bare except: blocks replaced with except Exception as e + logger
  - total_classes hardcode replaced with actual DB count
  - /enroll-section, /device-requests, approve/reject all now require auth
  - N+1 in get_my_attendance() fixed with bulk fetch pattern
  - Cloudinary upload errors now use logger instead of print()
  - get_my_timetable() N+1 fixed with bulk fetch
"""
import csv
import logging
import uuid
from datetime import datetime, timezone
from io import StringIO
from typing import Optional

import cloudinary.uploader
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, delete, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_admin, get_current_user, get_current_student
from app.core.security import get_password_hash, verify_password
from app.ml.predictor import ai_engine
from app.models.academic import (
    Subject, SubjectOffering, Timetable, Classroom,
    Section, Semester, SessionBatch, Degree, ClassSession,
)
from app.models.attendance import Avatar, AvatarMoodLog
from app.models.performance import (
    SessionalMark, AttendanceLog, StudentGamification,
    Assessment, StudentAssessmentRecord, PerformancePrediction, ResultStatus,
)
from app.models.system import Notification
from app.models.users import (
    User, Student, Lecturer, Admin, UserRole, AdminRole,
    DeviceChangeRequest, RequestStatus, UserDevice, DeviceStatus,
)

logger = logging.getLogger("iqrat.users")
router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — ONBOARDING
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/onboard/student")
async def onboard_student(
    full_name: str = Form(...),
    email: str = Form(...),
    roll_no: str = Form(...),
    degree_id: int = Form(None),
    section_id: int = Form(None),
    password: str = Form(...),
    contact_no: str = Form(None),
    photo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.email == email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Email already registered.")
    result = await db.execute(select(Student).where(Student.reg_no == roll_no))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Roll number already exists.")

    # Upload admission photo to Cloudinary
    try:
        upload_result = cloudinary.uploader.upload(
            photo.file,
            folder="iqrat_admission_photos",
            public_id=f"{roll_no}_master",
        )
        file_location = upload_result.get("secure_url")
    except Exception as e:
        logger.error("Cloudinary upload failed for student %s: %s", roll_no, e)
        raise HTTPException(status_code=500, detail="Failed to upload photo to cloud storage.")

    new_user = User(
        email=email,
        hashed_password=get_password_hash(password),
        role=UserRole.STUDENT,
        is_active=True,
        requires_password_change=True,   # Forces student to set their own password on first login
    )
    db.add(new_user)
    await db.flush()

    # Auto-deduce semester + session from section
    semester_id = None
    session_id = None
    if section_id:
        result = await db.execute(select(Section).where(Section.id == section_id))
        section = result.scalars().first()
        if section:
            semester_id = section.semester_id
            result = await db.execute(select(Semester).where(Semester.id == semester_id))
            semester = result.scalars().first()
            if semester:
                session_id = semester.session_id

    new_student = Student(
        user_id=new_user.id,
        full_name=full_name,
        reg_no=roll_no,
        degree_id=degree_id,
        section_id=section_id,
        semester_id=semester_id,
        session_id=session_id,
        photo_path=file_location,
        contact_no=contact_no,
        status="active",
    )
    db.add(new_student)
    await db.commit()

    # 🎓 NOTE: We log that an account was created, but NEVER log the password.
    #    Credentials must be communicated to the student via a secure channel
    #    (email in production). Printing passwords to server logs is a critical
    #    security violation — anyone with log access owns every account.
    logger.info("Student onboarded: %s (reg_no=%s)", email, roll_no)
    return {"msg": "Student onboarded successfully.", "student_id": new_student.id}


@router.post("/onboard/lecturer")
async def onboard_lecturer(
    full_name: str = Form(...),
    email: str = Form(...),
    employee_code: str = Form(...),
    department_id: int = Form(...),
    password: str = Form(...),
    contact_no: str = Form(None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.email == email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Email already taken.")

    new_user = User(
        email=email,
        hashed_password=get_password_hash(password),
        role=UserRole.LECTURER,
        is_active=True,
        requires_password_change=True,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    new_lecturer = Lecturer(
        user_id=new_user.id,
        full_name=full_name,
        employee_code=employee_code,
        department_id=department_id,
        contact_no=contact_no,
    )
    db.add(new_lecturer)
    await db.commit()

    logger.info("Lecturer onboarded: %s (emp=%s)", email, employee_code)
    return {"msg": "Lecturer onboarded successfully.", "lecturer_id": new_lecturer.id}


@router.post("/onboard/admin")
async def onboard_admin(
    full_name: str = Form(...),
    admin_id: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    contact_no: str = Form(None),
    role_level: str = Form(...),
    department_id: int = Form(None),
    permissions: str = Form(None),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    # Only 1 Super Admin allowed
    if role_level == "super_admin":
        result = await db.execute(select(Admin).where(Admin.role_level == AdminRole.SUPER_ADMIN))
        existing_super = result.scalars().first()
        if existing_super:
            raise HTTPException(status_code=400, detail="A Super Admin already exists.")

    result = await db.execute(select(User).where(User.email == email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Email already taken.")
    result = await db.execute(select(Admin).where(Admin.admin_id == admin_id))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Admin ID already exists.")

    new_user = User(
        email=email,
        hashed_password=get_password_hash(password),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(new_user)
    await db.flush()

    new_admin = Admin(
        user_id=new_user.id,
        admin_id=admin_id,
        full_name=full_name,
        role_level=role_level,
        department_id=department_id,
        contact_no=contact_no,
        permissions="ALL" if role_level == "super_admin" else permissions,
    )
    db.add(new_admin)
    await db.commit()

    logger.info("Admin onboarded: %s (level=%s)", email, role_level)
    return {
        "msg": f"{'Super' if role_level == 'super_admin' else 'Department'} Admin created successfully.",
        "admin_id": new_admin.id,
    }


@router.get("/next-admin-id")
async def get_next_admin_id(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(Admin.admin_id))
    admins = result.all()
    max_id = 0
    for (code,) in admins:
        try:
            if code and code.startswith("ADM-"):
                num = int(code.split("-")[1])
                if num > max_id:
                    max_id = num
        except (ValueError, IndexError) as e:
            logger.debug("Skipping malformed admin_id: %s (%s)", code, e)
    return {"next_admin_id": f"ADM-{str(max_id + 1).zfill(3)}"}


@router.post("/onboard/bulk")
async def onboard_bulk_users(
    role: str = Form(...),
    department_id: int = Form(...),
    degree_id: int = Form(None),
    batch_year: int = Form(None),
    batch_type: str = Form(None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Mass-enroll students or lecturers via CSV upload."""
    content = await file.read()
    decoded = content.decode("utf-8-sig")
    csv_reader = csv.DictReader(StringIO(decoded))

    success_count = 0
    error_count = 0

    for row in csv_reader:
        email = row.get("email", "").strip()
        full_name = row.get("full_name", "").strip()
        password = row.get("password", "iqrat123")

        if not email or not full_name:
            error_count += 1
            continue

        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalars().first():
            logger.debug("Bulk upload: skipping existing email %s", email)
            error_count += 1
            continue

        try:
            role_enum = UserRole.STUDENT if role.lower() == "student" else UserRole.LECTURER
            new_user = User(
                email=email,
                hashed_password=get_password_hash(password),
                role=role_enum,
                is_active=True,
                requires_password_change=True,
            )
            db.add(new_user)
            await db.flush()

            if role.lower() == "student":
                db.add(Student(
                    user_id=new_user.id,
                    full_name=full_name,
                    reg_no=row.get("roll_no", "").strip(),
                    department_id=department_id,
                    degree_id=degree_id,
                    status="active",
                ))
            else:
                db.add(Lecturer(
                    user_id=new_user.id,
                    full_name=full_name,
                    employee_code=row.get("employee_code", "").strip(),
                    department_id=department_id,
                ))

            success_count += 1

        except Exception as e:
            await db.rollback()
            logger.warning("Bulk upload error for %s: %s", email, e)
            error_count += 1

    await db.commit()
    logger.info("Bulk upload complete: %d success, %d failed", success_count, error_count)
    return {"msg": f"Upload complete: {success_count} enrolled, {error_count} failed or skipped."}


# ── User management ────────────────────────────────────────────────────────────

class UserEdit(BaseModel):
    full_name: str
    email: str
    contact_no: Optional[str] = None
    section_id: Optional[int] = None
    designation: Optional[str] = None
    permissions: Optional[str] = None


@router.get("/all")
async def get_all_users(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(Admin).where(Admin.user_id == current_admin.id))
    admin_profile = result.scalars().first()
    role_level = str(admin_profile.role_level).lower() if admin_profile else "super_admin"
    is_super = "super" in role_level
    dept_id = admin_profile.department_id if admin_profile else None

    users = []

    # Students
    students_q = select(Student, User.email, User.is_active).join(User)
    if not is_super and dept_id:
        students_q = students_q.join(Degree, Student.degree_id == Degree.id).where(Degree.department_id == dept_id)
    result = await db.execute(students_q)
    for s, email, is_act in result.all():
        users.append({"id": s.user_id, "name": s.full_name, "system_id": s.reg_no or "N/A", "role": "Student", "email": email, "status": "Active" if is_act else "Inactive", "lastLogin": "Recent"})

    # Lecturers
    lecturers_q = select(Lecturer, User.email, User.is_active).join(User)
    if not is_super and dept_id:
        lecturers_q = lecturers_q.where(Lecturer.department_id == dept_id)
    result = await db.execute(lecturers_q)
    for l, email, is_act in result.all():
        users.append({"id": l.user_id, "profile_id": l.id, "name": l.full_name, "system_id": l.employee_code or "N/A", "role": "Lecturer", "email": email, "status": "Active" if is_act else "Inactive", "lastLogin": "Recent"})

    if is_super:
        result = await db.execute(select(Admin, User.email, User.is_active).join(User))
        for a, email, is_act in result.all():
            users.append({"id": a.user_id, "name": a.full_name, "system_id": a.admin_id or "N/A", "role": "Admin", "email": email, "status": "Active" if is_act else "Inactive", "lastLogin": "Recent", "permissions": a.permissions or ""})

    return users


@router.put("/{user_id}")
async def edit_user(
    user_id: int,
    data: UserEdit,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    user.email = data.email

    if user.role == UserRole.STUDENT:
        result = await db.execute(select(Student).where(Student.user_id == user.id))
        profile = result.scalars().first()
        if profile:
            profile.full_name = data.full_name
            if data.contact_no:
                profile.contact_no = data.contact_no
            if data.section_id:
                profile.section_id = data.section_id
    elif user.role == UserRole.LECTURER:
        result = await db.execute(select(Lecturer).where(Lecturer.user_id == user.id))
        profile = result.scalars().first()
        if profile:
            profile.full_name = data.full_name
            if data.contact_no:
                profile.contact_no = data.contact_no
            if data.designation:
                profile.designation = data.designation
    elif user.role == UserRole.ADMIN:
        result = await db.execute(select(Admin).where(Admin.user_id == user_id))
        profile = result.scalars().first()
        if profile:
            profile.full_name = data.full_name
            if data.contact_no:
                profile.contact_no = data.contact_no
            if data.permissions is not None:
                profile.permissions = data.permissions

    await db.commit()
    return {"msg": "User updated successfully."}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    result = await db.execute(select(Student).where(Student.user_id == user_id))
    student = result.scalars().first()
    if student:
        await db.execute(delete(StudentGamification).where(StudentGamification.student_id == student.id))
        await db.execute(delete(DeviceChangeRequest).where(DeviceChangeRequest.student_id == student.id))
        await db.execute(delete(SessionalMark).where(SessionalMark.student_id == student.id))
        await db.execute(delete(StudentAssessmentRecord).where(StudentAssessmentRecord.student_id == student.id))
        await db.execute(delete(AttendanceLog).where(AttendanceLog.student_id == student.id))

    await db.execute(delete(Student).where(Student.user_id == user_id))
    await db.execute(delete(Lecturer).where(Lecturer.user_id == user_id))
    await db.execute(delete(Admin).where(Admin.user_id == user_id))
    await db.execute(delete(UserDevice).where(UserDevice.user_id == user_id))
    await db.execute(delete(Notification).where(Notification.user_id == user_id))
    await db.delete(user)
    await db.commit()

    logger.info("User %d deleted by admin %d", user_id, current_admin.id)
    return {"msg": "User and all associated records deleted."}


@router.get("/next-roll-no")
async def get_next_roll_no(
    degree_code: str,
    year: str,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(
        select(Student.reg_no).where(Student.reg_no.like(f"%-{degree_code.upper()}-{year}"))
    )
    students = result.all()
    max_id = 0
    for (reg_no,) in students:
        try:
            curr_id = int(reg_no.split("-")[0])
            if curr_id > max_id:
                max_id = curr_id
        except (ValueError, IndexError):
            continue
    next_id = str(max_id + 1).zfill(4)
    return {"next_roll_no": f"{next_id}-{degree_code.upper()}-{year}", "numeric_id": max_id + 1}


@router.get("/next-emp-id")
async def get_next_emp_id(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(Lecturer.employee_code))
    lecturers = result.all()
    max_id = 0
    for (code,) in lecturers:
        try:
            if code and code.startswith("EMP-"):
                num = int(code.split("-")[1])
                if num > max_id:
                    max_id = num
        except (ValueError, IndexError) as e:
            logger.debug("Skipping malformed employee_code: %s (%s)", code, e)
    return {"next_emp_id": f"EMP-{str(max_id + 1).zfill(3)}"}


# ── Enrollment ─────────────────────────────────────────────────────────────────

@router.post("/enroll-section")
async def batch_enroll_section(
    data: dict,
    db: AsyncSession = Depends(get_db),
    # 🎓 Previously this had NO auth — anyone could enroll any student into
    #    any subject with a single unauthenticated POST. Fixed.
    current_admin: User = Depends(get_current_admin),
):
    semester_id = data.get("semester_id")
    section_id = data.get("section_id")
    subject_id = data.get("subject_id")

    result = await db.execute(select(Student).where(Student.section_id == section_id))
    students = result.scalars().all()
    enrolled_count = 0
    for student in students:
        result = await db.execute(
            select(SessionalMark).where(
                SessionalMark.student_id == student.id,
                SessionalMark.subject_id == subject_id,
                SessionalMark.semester_id == semester_id,
            )
        )
        exists = result.scalars().first()
        if not exists:
            db.add(SessionalMark(
                student_id=student.id,
                subject_id=subject_id,
                semester_id=semester_id,
                midterm_marks=0,
                total_sessional_marks=0,
            ))
            enrolled_count += 1

    await db.commit()
    return {"msg": f"Successfully enrolled {enrolled_count} students (skipped existing)."}


class SingleEnrollRequest(BaseModel):
    reg_no: str
    subject_id: int
    semester_id: int
    section_id: Optional[int] = None


@router.post("/enroll-repeat")
async def enroll_repeat_subject(
    data: SingleEnrollRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(Student).where(Student.reg_no == data.reg_no))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student roll number not found.")

    result = await db.execute(
        select(SessionalMark).where(
            SessionalMark.student_id == student.id,
            SessionalMark.subject_id == data.subject_id,
            SessionalMark.semester_id == data.semester_id,
        )
    )
    exists = result.scalars().first()
    if exists:
        raise HTTPException(status_code=400, detail="Student already enrolled in this subject for this semester.")

    db.add(SessionalMark(
        student_id=student.id,
        subject_id=data.subject_id,
        semester_id=data.semester_id,
        midterm_marks=0,
        total_sessional_marks=0,
    ))
    await db.commit()
    return {"msg": f"Successfully enrolled {student.reg_no} in repeat subject."}


# ══════════════════════════════════════════════════════════════════════════════
# STUDENT — /me/* ENDPOINTS
#
# 🎓 THE FIX: Every endpoint below uses `current_user: User = Depends(get_current_user)`
#    instead of `email: str` as a query param.
#
#    Before: GET /me/courses?email=victim@uni.edu  → anyone could read anyone's data
#    After:  GET /me/courses  → server reads email from the verified JWT token
#
#    The frontend change is simple: remove the ?email= param from every fetch() call.
#    The JWT already contains the email — the server extracts it server-side.
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/me/courses")
async def get_my_courses(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found.")

    result = await db.execute(select(SessionalMark).where(SessionalMark.student_id == student.id))
    enrollments = result.scalars().all()
    if not enrollments:
        return []

    # ── Bulk fetch (N+1 fix) ──────────────────────────────────────────────────
    subject_ids  = [e.subject_id  for e in enrollments]
    semester_ids = [e.semester_id for e in enrollments]

    result = await db.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    all_subjects  = {s.id: s for s in result.scalars().all()}
    result = await db.execute(
        select(SubjectOffering).where(
            SubjectOffering.subject_id.in_(subject_ids),
            SubjectOffering.semester_id.in_(semester_ids),
        )
    )
    all_offerings = result.scalars().all()
    lec_ids      = [o.lecturer_id for o in all_offerings if o.lecturer_id]
    result = await db.execute(select(Lecturer).where(Lecturer.id.in_(lec_ids)))
    all_lecturers = {l.id: l for l in result.scalars().all()}
    offering_ids  = [o.id for o in all_offerings]
    result = await db.execute(select(Timetable).where(Timetable.offering_id.in_(offering_ids)))
    all_tts       = result.scalars().all()
    tt_ids        = [t.id for t in all_tts]
    result = await db.execute(
        select(AttendanceLog).where(
            AttendanceLog.student_id == student.id,
            AttendanceLog.timetable_id.in_(tt_ids),
            AttendanceLog.status == "Present",
        )
    )
    all_logs      = result.scalars().all()
    # ─────────────────────────────────────────────────────────────────────────

    courses_data = []
    for enr in enrollments:
        sub = all_subjects.get(enr.subject_id)
        if not sub:
            continue

        offering = next(
            (o for o in all_offerings if o.subject_id == sub.id and o.semester_id == enr.semester_id),
            None,
        )
        lecturer_name = "TBD"
        presents      = 0

        if offering:
            lec = all_lecturers.get(offering.lecturer_id)
            if lec:
                lecturer_name = lec.full_name
            offering_tt_ids = {t.id for t in all_tts if t.offering_id == offering.id}
            presents = sum(1 for log in all_logs if log.timetable_id in offering_tt_ids)

        # 🎓 total_classes was hardcoded to 30 — now we count actual scheduled sessions
        #    from the Timetable. This is still an approximation (timetable slots ≠ held
        #    sessions) but far more accurate. Phase 1 will use ClassSession records.
        total_classes = len([t for t in all_tts if offering and t.offering_id == offering.id]) or 1
        attendance_pct = min(int((presents / total_classes) * 100), 100)

        courses_data.append({
            "offering_id":   offering.id if offering else None,
            "name":          sub.name,
            "code":          sub.code,
            "section":       f"Section {student.section_id}",
            "lecturer":      lecturer_name,
            "attendance":    attendance_pct,
            "presents":      presents,
            "absents":       max(total_classes - presents, 0),
            "leaves":        0,
        })

    return courses_data


@router.get("/me/timetable")
async def get_my_timetable(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found.")

    result = await db.execute(select(SessionalMark).where(SessionalMark.student_id == student.id))
    enrollments = result.scalars().all()
    if not enrollments:
        return []

    # ── Bulk fetch (replaces the N+1 loop from the original) ─────────────────
    subject_ids  = [e.subject_id  for e in enrollments]
    semester_ids = [e.semester_id for e in enrollments]

    result = await db.execute(
        select(SubjectOffering).where(
            SubjectOffering.subject_id.in_(subject_ids),
            SubjectOffering.semester_id.in_(semester_ids),
        )
    )
    all_offerings = result.scalars().all()
    offering_ids  = [o.id for o in all_offerings]
    result = await db.execute(select(Timetable).where(Timetable.offering_id.in_(offering_ids)))
    all_tts       = result.scalars().all()
    classroom_ids = [t.classroom_id for t in all_tts]
    result = await db.execute(select(Classroom).where(Classroom.id.in_(classroom_ids)))
    all_rooms     = {r.id: r for r in result.scalars().all()}
    result = await db.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    all_subjects  = {s.id: s for s in result.scalars().all()}
    lec_ids       = [o.lecturer_id for o in all_offerings if o.lecturer_id]
    result = await db.execute(select(Lecturer).where(Lecturer.id.in_(lec_ids)))
    all_lecturers = {l.id: l for l in result.scalars().all()}
    offerings_map = {o.id: o for o in all_offerings}
    # ─────────────────────────────────────────────────────────────────────────

    schedule = []
    for slot in all_tts:
        off  = offerings_map.get(slot.offering_id)
        sub  = all_subjects.get(off.subject_id) if off else None
        lec  = all_lecturers.get(off.lecturer_id) if off else None
        room = all_rooms.get(slot.classroom_id)

        schedule.append({
            "id":      slot.id,
            "day":     slot.day_of_week,
            "start":   slot.start_time.strftime("%I:%M %p"),
            "end":     slot.end_time.strftime("%I:%M %p"),
            "subject": sub.name if sub else "Unknown",
            "code":    sub.code if sub else "---",
            "teacher": lec.full_name if lec else "TBD",
            "room":    room.room_no if room else "TBD",
        })

    return schedule


@router.get("/me/lecturer/courses")
async def get_lecturer_courses(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
    lecturer = result.scalars().first()
    if not lecturer:
        raise HTTPException(status_code=404, detail="Lecturer profile not found.")

    result = await db.execute(select(SubjectOffering).where(SubjectOffering.lecturer_id == lecturer.id))
    offerings = result.scalars().all()
    subject_ids = [o.subject_id for o in offerings]
    result = await db.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    subjects_map = {s.id: s for s in result.scalars().all()}

    return [
        {
            "id":   off.id,
            "code": subjects_map[off.subject_id].code if off.subject_id in subjects_map else "---",
            "name": subjects_map[off.subject_id].name if off.subject_id in subjects_map else "Unknown",
            "section": "All Sections",
        }
        for off in offerings
        if off.subject_id in subjects_map
    ]


@router.get("/me/lecturer/timetable")
async def get_lecturer_timetable(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
    lecturer = result.scalars().first()
    if not lecturer:
        raise HTTPException(status_code=404, detail="Lecturer profile not found.")

    result = await db.execute(select(SubjectOffering).where(SubjectOffering.lecturer_id == lecturer.id))
    offerings = result.scalars().all()
    offering_ids  = [o.id for o in offerings]
    if not offering_ids:
        return []

    result = await db.execute(select(Timetable).where(Timetable.offering_id.in_(offering_ids)))
    slots        = result.scalars().all()
    subject_ids  = [o.subject_id for o in offerings]
    result = await db.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    all_subjects = {s.id: s for s in result.scalars().all()}
    
    classroom_ids = [s.classroom_id for s in slots]
    result = await db.execute(select(Classroom).where(Classroom.id.in_(classroom_ids)))
    all_rooms    = {c.id: c for c in result.scalars().all()}
    offerings_map = {o.id: o for o in offerings}

    schedule = []
    for slot in slots:
        off  = offerings_map.get(slot.offering_id)
        sub  = all_subjects.get(off.subject_id) if off else None
        room = all_rooms.get(slot.classroom_id)
        schedule.append({
            "timetable_id": slot.id,
            "offering_id":  off.id if off else None,
            "day":          slot.day_of_week,
            "start":        slot.start_time.strftime("%I:%M %p"),
            "end":          slot.end_time.strftime("%I:%M %p"),
            "code":         sub.code if sub else "---",
            "name":         sub.name if sub else "Unknown",
            "room":         room.room_no if room else "TBD",
            "type":         "Lecture",
        })

    return schedule


@router.get("/me/attendance")
async def get_my_attendance(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found.")

    result = await db.execute(
        select(AttendanceLog)
        .where(AttendanceLog.student_id == student.id)
        .order_by(AttendanceLog.scan_time.desc())
    )
    logs = result.scalars().all()
    if not logs:
        return []

    # ── Bulk fetch to kill the per-log N+1 chain ──────────────────────────────
    tt_ids   = list({log.timetable_id for log in logs if log.timetable_id})
    result = await db.execute(select(Timetable).where(Timetable.id.in_(tt_ids)))
    all_tts  = {t.id: t for t in result.scalars().all()}

    off_ids   = list({t.offering_id for t in all_tts.values() if t.offering_id})
    result = await db.execute(select(SubjectOffering).where(SubjectOffering.id.in_(off_ids)))
    all_offs  = {o.id: o for o in result.scalars().all()}

    sub_ids   = list({o.subject_id for o in all_offs.values()})
    result = await db.execute(select(Subject).where(Subject.id.in_(sub_ids)))
    all_subs  = {s.id: s for s in result.scalars().all()}

    # Session IDs for date fallback
    session_ids = list({log.session_id for log in logs if log.session_id and log.scan_time is None})
    all_sessions = {}
    if session_ids:
        # session_id is stored as string in AttendanceLog
        int_ids = []
        for sid in session_ids:
            try:
                int_ids.append(int(sid))
            except (ValueError, TypeError):
                pass
        if int_ids:
            result = await db.execute(select(ClassSession).where(ClassSession.id.in_(int_ids)))
            all_sessions = {
                str(s.id): s
                for s in result.scalars().all()
            }
    # ─────────────────────────────────────────────────────────────────────────

    history = []
    for i, log in enumerate(logs):
        sub_code = "---"
        tt = all_tts.get(log.timetable_id)
        if tt:
            off = all_offs.get(tt.offering_id)
            if off:
                sub = all_subs.get(off.subject_id)
                if sub:
                    sub_code = sub.code

        if log.scan_time:
            display_date = log.scan_time.strftime("%b %d, %Y • %I:%M %p")
        else:
            session = all_sessions.get(log.session_id)
            display_date = (
                session.session_date.strftime("%b %d, %Y")
                if session and session.session_date
                else "Unknown Date"
            )

        history.append({
            "sr":           len(logs) - i,
            "date":         display_date,
            "status":       log.status,
            "subject_code": sub_code,
        })

    return history


@router.get("/me/dashboard-stats")
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found.")

    result = await db.execute(
        select(StudentGamification).where(StudentGamification.student_id == student.id)
    )
    gamification = result.scalars().first()
    if not gamification:
        gamification = StudentGamification(student_id=student.id, xp_points=0, current_streak=0)
        db.add(gamification)
        await db.commit()
        await db.refresh(gamification)

    level = (gamification.xp_points // 100) + 1
    if level <= 5:       badge = "Novice Learner"
    elif level <= 15:    badge = "Scholar"
    elif level <= 49:    badge = "Dean's List Elite"
    else:                badge = "Legend of GCU"

    result = await db.execute(select(AttendanceLog).where(AttendanceLog.student_id == student.id))
    all_logs      = result.scalars().all()
    total_scans   = len(all_logs)
    total_presents = sum(1 for l in all_logs if l.status == "Present")
    avg_attendance = int((total_presents / total_scans) * 100) if total_scans > 0 else 100

    # Avatar mood logic
    if total_scans == 0:         new_mood = "focused"
    elif avg_attendance >= 90:   new_mood = "happy"
    elif avg_attendance >= 80:   new_mood = "improving"
    elif avg_attendance >= 70:   new_mood = "focused"
    elif avg_attendance >= 60:   new_mood = "lowering"
    else:                        new_mood = "stressed"

    result = await db.execute(select(Avatar).where(Avatar.student_id == student.id))
    avatar = result.scalars().first()
    if not avatar:
        avatar = Avatar(student_id=student.id, avatar_style=new_mood, level=level, xp_points=gamification.xp_points)
        db.add(avatar)
        await db.commit()
        await db.refresh(avatar)

    if avatar.avatar_style != new_mood:
        db.add(AvatarMoodLog(
            avatar_id=avatar.id,
            mood=new_mood,
            trigger_reason=f"Attendance shifted to {avg_attendance}%",
        ))
        avatar.avatar_style = new_mood
        await db.commit()

    # Leaderboard — bulk fetch to kill N+1
    result = await db.execute(select(Student).where(Student.section_id == student.section_id))
    section_student_ids = [s.id for s in result.scalars().all()]
    
    result = await db.execute(
        select(StudentGamification)
        .where(StudentGamification.student_id.in_(section_student_ids))
        .order_by(StudentGamification.xp_points.desc())
    )
    all_gami = result.scalars().all()
    my_rank = next((i + 1 for i, g in enumerate(all_gami) if g.student_id == student.id), 1)

    # Bulk fetch top-10 students in one query instead of looping db.query per student
    top_ids     = [g.student_id for g in all_gami[:10]]
    result = await db.execute(select(Student).where(Student.id.in_(top_ids)))
    top_students = {s.id: s for s in result.scalars().all()}
    top_10 = [
        {
            "name": top_students[g.student_id].full_name if g.student_id in top_students else "---",
            "roll": top_students[g.student_id].reg_no   if g.student_id in top_students else "---",
            "xp":   g.xp_points,
            "rank": i + 1,
        }
        for i, g in enumerate(all_gami[:10])
    ]
    while len(top_10) < 3:
        top_10.append({"name": "---", "roll": "---", "xp": 0, "rank": len(top_10) + 1})

    return {
        "xp_points":       gamification.xp_points,
        "level":           level,
        "badge":           badge,
        "current_streak":  gamification.current_streak,
        "avg_attendance":  avg_attendance,
        "rank":            my_rank,
        "top_10_students": top_10,
        "current_mood":    avatar.avatar_style,
    }


@router.get("/me/grades")
async def get_my_grades(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=404, detail="Student profile not found.")

    result = await db.execute(select(SessionalMark).where(SessionalMark.student_id == student.id))
    enrollments = result.scalars().all()
    if not enrollments:
        return []

    # ── Bulk fetch (N+1 fix) ──────────────────────────────────────────────────
    subject_ids  = [e.subject_id  for e in enrollments]
    semester_ids = [e.semester_id for e in enrollments]

    result = await db.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    all_subjects = {s.id: s for s in result.scalars().all()}

    result = await db.execute(
        select(Assessment).where(
            Assessment.subject_id.in_(subject_ids),
            Assessment.semester_id.in_(semester_ids),
        )
    )
    all_assessments = result.scalars().all()
    assessment_ids = [a.id for a in all_assessments]

    # Key: (subject_id, semester_id) -> list of assessments
    assessments_by_enr: dict = {}
    for a in all_assessments:
        assessments_by_enr.setdefault((a.subject_id, a.semester_id), []).append(a)

    _result = await db.execute(
        select(StudentAssessmentRecord).where(
            StudentAssessmentRecord.assessment_id.in_(assessment_ids),
            StudentAssessmentRecord.student_id == student.id,
        )
    )
    all_records = {
        r.assessment_id: r
        for r in _result.scalars().all()
    }
    # ─────────────────────────────────────────────────────────────────────────

    result = []
    for enr in enrollments:
        subject = all_subjects.get(enr.subject_id)
        if not subject:
            continue

        assessments = assessments_by_enr.get((subject.id, enr.semester_id), [])
        marks_data = []
        for ass in assessments:
            record = all_records.get(ass.id)
            marks_data.append({
                "id":                 ass.id,
                "name":               ass.name,
                "category":           ass.category,
                "total_marks":        ass.max_marks,
                "obtained_marks":     record.obtained_marks if record and record.obtained_marks is not None else None,
                "lecturer_file_path": ass.file_path,
                "status":             record.status if record else "Pending",
            })

        result.append({
            "subject_id":   subject.id,
            "subject_code": subject.code,
            "subject_name": subject.name,
            "assessments":  marks_data,
        })

    return result


@router.get("/me/profile")
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.STUDENT:
        result = await db.execute(select(Student).where(Student.user_id == current_user.id))
        profile = result.scalars().first()
        identifier = profile.reg_no if profile else ""
    elif current_user.role == UserRole.LECTURER:
        result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
        profile = result.scalars().first()
        identifier = profile.employee_code if profile else ""
    elif current_user.role == UserRole.ADMIN:
        result = await db.execute(select(Admin).where(Admin.user_id == current_user.id))
        profile = result.scalars().first()
        identifier = profile.admin_id if profile else ""
    else:
        raise HTTPException(status_code=403, detail="Unknown role.")

    return {
        "full_name":                  getattr(profile, "full_name", ""),
        "email":                      current_user.email,
        "reg_no":                     identifier,
        "contact_no":                 getattr(profile, "contact_no", ""),
        "photo_path":                 getattr(profile, "photo_path", None),
        "theme_preference":           getattr(profile, "theme_preference", "default"),
        "notify_class_reminders":     getattr(profile, "notify_class_reminders", True),
        "notify_assignment_deadlines": getattr(profile, "notify_assignment_deadlines", True),
    }


@router.put("/me/profile")
async def update_my_profile(
    full_name: str = Form(...),
    new_email: str = Form(...),
    contact_no: str = Form(""),
    photo: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Email uniqueness check (excluding self)
    if new_email != current_user.email:
        result = await db.execute(select(User).where(User.email == new_email))
        if result.scalars().first():
            raise HTTPException(status_code=400, detail="Email already in use.")
        current_user.email = new_email

    if current_user.role == UserRole.STUDENT:
        result = await db.execute(select(Student).where(Student.user_id == current_user.id))
        profile = result.scalars().first()
        identifier = profile.reg_no if profile else str(current_user.id)
    elif current_user.role == UserRole.LECTURER:
        result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
        profile = result.scalars().first()
        identifier = profile.employee_code if profile else str(current_user.id)
    elif current_user.role == UserRole.ADMIN:
        result = await db.execute(select(Admin).where(Admin.user_id == current_user.id))
        profile = result.scalars().first()
        identifier = profile.admin_id if profile else str(current_user.id)
    else:
        raise HTTPException(status_code=403, detail="Unknown role.")

    profile.full_name = full_name
    profile.contact_no = contact_no

    if photo:
        try:
            upload_result = cloudinary.uploader.upload(
                photo.file,
                folder="iqrat_profile_photos",
                public_id=f"{identifier}_profile",
                invalidate=True,
            )
            if hasattr(profile, "photo_path"):
                profile.photo_path = upload_result.get("secure_url")
        except Exception as e:
            logger.error("Cloudinary profile photo update failed for %s: %s", identifier, e)
            raise HTTPException(status_code=500, detail="Failed to update photo in cloud storage.")

    await db.commit()
    return {"msg": "Profile updated successfully.", "new_email": current_user.email}


class SettingsUpdate(BaseModel):
    theme_preference: str
    notify_class_reminders: bool
    notify_assignment_deadlines: bool


@router.put("/me/settings")
async def update_settings(
    data: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.STUDENT:
        result = await db.execute(select(Student).where(Student.user_id == current_user.id))
        profile = result.scalars().first()
    elif current_user.role == UserRole.LECTURER:
        result = await db.execute(select(Lecturer).where(Lecturer.user_id == current_user.id))
        profile = result.scalars().first()
    elif current_user.role == UserRole.ADMIN:
        # Admins don't have theme/notification prefs stored — return success silently
        return {"msg": "Settings saved successfully."}
    else:
        raise HTTPException(status_code=403, detail="Unknown role.")

    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    profile.theme_preference            = data.theme_preference
    profile.notify_class_reminders      = data.notify_class_reminders
    profile.notify_assignment_deadlines = data.notify_assignment_deadlines
    await db.commit()
    return {"msg": "Settings saved successfully."}


class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str


@router.put("/me/password")
async def update_password(
    passwords: PasswordUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(passwords.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect current password.")
    current_user.hashed_password = get_password_hash(passwords.new_password)
    await db.commit()
    return {"msg": "Password updated successfully."}


@router.get("/me/notifications")
async def get_my_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.sent_at.desc())
        .limit(20)
    )
    alerts = result.scalars().all()
    return [
        {
            "id":       a.id,
            "title":    a.title,
            "message":  a.message,
            "is_read":  a.is_read,
            "time":     a.sent_at.strftime("%b %d, %I:%M %p") if a.sent_at else "Just now",
        }
        for a in alerts
    ]


class MarkReadRequest(BaseModel):
    notification_ids: list[int]


@router.put("/me/notifications/read")
async def mark_notifications_read(
    req: MarkReadRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 🎓 We filter by user_id AND id — prevents a user marking another user's
    #    notifications as read by guessing notification IDs.
    await db.execute(
        update(Notification)
        .where(
            Notification.id.in_(req.notification_ids),
            Notification.user_id == current_user.id,
        )
        .values(is_read=True)
    )
    await db.commit()
    return {"msg": "Notifications marked as read."}


# ── Device management ──────────────────────────────────────────────────────────

@router.get("/device-requests")
async def get_device_requests(
    db: AsyncSession = Depends(get_db),
    # Previously no auth at all — any unauthenticated request could read all device data
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(DeviceChangeRequest).where(DeviceChangeRequest.status == RequestStatus.PENDING))
    requests = result.scalars().all()
    student_ids = [r.student_id for r in requests]
    students_result = await db.execute(select(Student).where(Student.id.in_(student_ids)))
    students_map = {s.id: s for s in students_result.scalars().all()}
    
    result_list = []
    for req in requests:
        student = students_map.get(req.student_id)
        if student:
            result_list.append({
                "id":     req.id,
                "name":   student.full_name,
                "id_no":  student.reg_no,
                "role":   "Student",
                "device": req.new_device_fingerprint[:12] + "...",
                "reason": req.reason or "Change requested",
                "date":   req.requested_at.strftime("%Y-%m-%d") if req.requested_at else "Today",
            })
    return result_list


@router.post("/device-requests/{req_id}/approve")
async def approve_device_request(
    req_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(DeviceChangeRequest).where(DeviceChangeRequest.id == req_id))
    req = result.scalars().first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found.")

    req.status = RequestStatus.APPROVED
    result = await db.execute(select(Student).where(Student.id == req.student_id))
    student = result.scalars().first()
    if student:
        result = await db.execute(select(UserDevice).where(UserDevice.user_id == student.user_id))
        for d in result.scalars().all():
            d.status = DeviceStatus.REVOKED
        db.add(UserDevice(
            user_id=student.user_id,
            device_fingerprint=req.new_device_fingerprint,
            status=DeviceStatus.ACTIVE,
        ))
    await db.commit()
    return {"msg": "Device approved."}


@router.post("/device-requests/{req_id}/reject")
async def reject_device_request(
    req_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    result = await db.execute(select(DeviceChangeRequest).where(DeviceChangeRequest.id == req_id))
    req = result.scalars().first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found.")
    req.status = RequestStatus.REJECTED
    await db.commit()
    return {"msg": "Device rejected."}


class DeviceChangeReq(BaseModel):
    new_device_fingerprint: str
    reason: str


@router.post("/me/device-request")
async def request_device_change(
    req: DeviceChangeReq,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=403, detail="Only students can request device changes.")

    result = await db.execute(
        select(DeviceChangeRequest).where(
            DeviceChangeRequest.student_id == student.id,
            DeviceChangeRequest.status == RequestStatus.PENDING,
        )
    )
    existing = result.scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a pending device request.")

    db.add(DeviceChangeRequest(
        student_id=student.id,
        new_device_fingerprint=req.new_device_fingerprint,
        reason=req.reason,
        status=RequestStatus.PENDING,
    ))
    await db.commit()
    return {"msg": "Device change request submitted. Awaiting admin approval."}


# ── Assignment submission — Cloudinary (was local disk) ───────────────────────

@router.post("/me/submit-assignment")
async def submit_assignment(
    assessment_id: int = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Uploads student submission directly to Cloudinary.

    🎓 Why Cloudinary instead of local disk?
    Railway and Render use ephemeral filesystems — any file written to local disk
    is DELETED on every deploy or container restart. Cloudinary is persistent,
    CDN-backed, and free up to 25GB. All student work is safe regardless of
    how many times we redeploy.
    """
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        raise HTTPException(status_code=403, detail="Only students can submit assignments.")

    result = await db.execute(
        select(StudentAssessmentRecord).where(
            StudentAssessmentRecord.assessment_id == assessment_id,
            StudentAssessmentRecord.student_id == student.id,
        )
    )
    record = result.scalars().first()
    if not record:
        raise HTTPException(status_code=404, detail="Assessment record not found.")
    if record.status == "Graded":
        raise HTTPException(status_code=400, detail="Cannot resubmit an already graded assignment.")

    # Build a unique public_id so filenames don't collide
    safe_name = f"submissions/{student.reg_no}_{assessment_id}_{uuid.uuid4().hex[:8]}"

    try:
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="iqrat_submissions",
            public_id=safe_name,
            resource_type="raw",   # "raw" allows any file type (PDF, DOCX, ZIP, etc.)
        )
        file_url = upload_result.get("secure_url")
    except Exception as e:
        logger.error("Cloudinary submission upload failed for student %s: %s", student.reg_no, e)
        raise HTTPException(status_code=500, detail="Failed to upload submission. Please try again.")

    record.submitted_file_path = file_url
    record.submitted_at        = datetime.now(timezone.utc)
    record.status              = "Submitted"
    await db.commit()

    logger.info("Assignment submitted: student=%s, assessment=%d", student.reg_no, assessment_id)
    return {"msg": "Assignment submitted successfully.", "file_url": file_url}


# ── AI Predictions ─────────────────────────────────────────────────────────────

@router.get("/me/predictions")
async def get_ai_predictions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Student).where(Student.user_id == current_user.id))
    student = result.scalars().first()
    if not student:
        return []

    result = await db.execute(select(SessionalMark).where(SessionalMark.student_id == student.id))
    enrollments = result.scalars().all()
    if not enrollments:
        return []

    # ── Bulk fetch outer data (subjects, cached predictions) ─────────────────
    subject_ids  = [e.subject_id  for e in enrollments]
    semester_ids = [e.semester_id for e in enrollments]

    result = await db.execute(select(Subject).where(Subject.id.in_(subject_ids)))
    all_subjects = {s.id: s for s in result.scalars().all()}

    result = await db.execute(
        select(PerformancePrediction).where(
            PerformancePrediction.student_id == student.id,
            PerformancePrediction.subject_id.in_(subject_ids),
        )
    )
    cached_preds = {
        p.subject_id: p
        for p in result.scalars().all()
    }

    result = await db.execute(
        select(Assessment).where(
            Assessment.subject_id.in_(subject_ids),
            Assessment.semester_id.in_(semester_ids),
        )
    )
    all_assessments = result.scalars().all()
    assessment_ids = [a.id for a in all_assessments]
    assessments_by_enr: dict = {}
    for a in all_assessments:
        assessments_by_enr.setdefault((a.subject_id, a.semester_id), []).append(a)

    result = await db.execute(
        select(StudentAssessmentRecord).where(
            StudentAssessmentRecord.assessment_id.in_(assessment_ids),
            StudentAssessmentRecord.student_id == student.id,
        )
    )
    all_records = {
        r.assessment_id: r
        for r in result.scalars().all()
    }
    # ─────────────────────────────────────────────────────────────────────────

    predictions_data = []

    for enr in enrollments:
        subject = all_subjects.get(enr.subject_id)
        if not subject:
            continue

        # Fast path — return cached prediction if already computed
        existing_pred = cached_preds.get(subject.id)
        if existing_pred:
            predictions_data.append({
                "subject_code":          subject.code,
                "subject_name":          subject.name,
                "predicted_attendance":  0,
                "predicted_score":       float(existing_pred.predicted_score),
                "sessional_score":       0,
                "final_exam_prediction": 0,
                "status":                existing_pred.predicted_status.value,
            })
            continue

        # Build attendance sequence
        result = await db.execute(
            select(SubjectOffering.id).where(
                SubjectOffering.subject_id == subject.id,
                SubjectOffering.semester_id == enr.semester_id,
            )
        )
        offerings = result.all()
        offering_ids = [o[0] for o in offerings]

        logs = []
        if offering_ids:
            result = await db.execute(
                select(AttendanceLog)
                .join(Timetable, AttendanceLog.timetable_id == Timetable.id)
                .where(
                    AttendanceLog.student_id == student.id,
                    Timetable.offering_id.in_(offering_ids),
                )
                .order_by(AttendanceLog.scan_time.asc())
            )
            logs = result.scalars().all()

        att_seq = [1 if log.status == "Present" else 0 for log in logs]
        predicted_att_pct = ai_engine.predict_attendance(att_seq) if att_seq else 100.0

        assessments = assessments_by_enr.get((subject.id, enr.semester_id), [])
        sessional_max      = 0.0
        sessional_obtained = 0.0
        for ass in assessments:
            record = all_records.get(ass.id)
            obtained = record.obtained_marks if record and record.obtained_marks is not None else 0.0
            if ass.category.lower() != "exam":
                sessional_max      += ass.max_marks
                sessional_obtained += obtained

        # Skip prediction if there's literally no data yet
        if len(logs) == 0 and sessional_max == 0.0:
            continue

        scaled_sessional    = (sessional_obtained / sessional_max * 50.0) if sessional_max > 0 else 0.0
        predicted_final_exam = ai_engine.predict_grade(sessional_obtained, sessional_max, predicted_att_pct)
        predicted_total      = scaled_sessional + predicted_final_exam

        pred_status = ResultStatus.PASS
        if scaled_sessional < 25.0 or predicted_final_exam < 25.0:
            pred_status = ResultStatus.FAIL
        elif predicted_total < 50.0 or predicted_att_pct < 75.0:
            pred_status = ResultStatus.AT_RISK

        try:
            result = await db.execute(
                select(PerformancePrediction).where(
                    PerformancePrediction.student_id == student.id,
                    PerformancePrediction.subject_id == subject.id,
                )
            )
            pred_record = result.scalars().first()
            if not pred_record:
                db.add(PerformancePrediction(
                    student_id=student.id,
                    subject_id=subject.id,
                    predicted_status=pred_status,
                    predicted_score=predicted_total,
                    confidence_score=0.85,
                    model_version="v1.1-Strict5050",
                ))
            else:
                pred_record.predicted_status = pred_status
                pred_record.predicted_score  = predicted_total
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning("Prediction DB sync warning for student %s, subject %s: %s",
                           student.reg_no, subject.code, e)

        predictions_data.append({
            "subject_code":          subject.code,
            "subject_name":          subject.name,
            "predicted_attendance":  round(predicted_att_pct, 1),
            "predicted_score":       round(predicted_total, 1),
            "sessional_score":       round(scaled_sessional, 1),
            "final_exam_prediction": round(predicted_final_exam, 1),
            "status":                pred_status.value,
        })

    return predictions_data