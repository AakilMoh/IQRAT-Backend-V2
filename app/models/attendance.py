from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Float, Enum, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base_class import Base
import enum

class AttendanceStatus(str, enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    MANUAL = "manual"

class TokenStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    class_session_id = Column(Integer, ForeignKey("class_sessions.id"), nullable=False)
    
    status = Column(Enum(AttendanceStatus), default=AttendanceStatus.ABSENT)
    scan_time = Column(DateTime(timezone=True), nullable=True)
    verified_geo = Column(Boolean, default=False)
    
    # Anti-Fraud & Location
    location_lat = Column(Float, nullable=True)
    location_long = Column(Float, nullable=True)
    photo_url = Column(String, nullable=True)
    remarks = Column(String, nullable=True)
    
    student = relationship("Student", back_populates="attendances")
    class_session = relationship("ClassSession", back_populates="attendances")
    device_log = relationship("DeviceLog", back_populates="attendance", uselist=False)

class DeviceLog(Base):
    __tablename__ = "device_logs"
    id = Column(Integer, primary_key=True, index=True)
    attendance_id = Column(Integer, ForeignKey("attendance.id"), nullable=False)
    
    device_fingerprint = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    browser_name = Column(String, nullable=True)
    
    attendance = relationship("Attendance", back_populates="device_log")

# --- GAMIFICATION ---
class Avatar(Base):
    __tablename__ = "avatars"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), unique=True, nullable=False)
    
    avatar_name = Column(String, default="Novice Scholar")
    avatar_style = Column(String, nullable=True) # e.g. "robot_v1"
    level = Column(Integer, default=1)
    xp_points = Column(Integer, default=0)
    current_streak = Column(Integer, default=0)
    
    student = relationship("Student", back_populates="avatar")
    mood_logs = relationship("AvatarMoodLog", back_populates="avatar")

class AvatarMoodLog(Base):
    __tablename__ = "avatar_mood_logs"
    id = Column(Integer, primary_key=True, index=True)
    avatar_id = Column(Integer, ForeignKey("avatars.id"), nullable=False)
    mood = Column(String, nullable=False) # happy, sad
    trigger_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    avatar = relationship("Avatar", back_populates="mood_logs")

class Leaderboard(Base):
    __tablename__ = "leaderboard"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=False)
    rank_position = Column(Integer, nullable=False)
    score = Column(Float, nullable=False)
    
    student = relationship("Student", back_populates="leaderboard_entry")

class ExceptionLog(Base):
    __tablename__ = "exception_logs"
    id = Column(Integer, primary_key=True, index=True)
    attendance_id = Column(Integer, ForeignKey("attendance.id"), nullable=False)
    raised_by = Column(Integer, ForeignKey("users.id"), nullable=False) # Student or Admin
    reason = Column(String, nullable=True)
    resolution_status = Column(String, default="pending") # pending, resolved, rejected
    
    # Note: Add 'exception_logs' relationship to Attendance class if needed, or just link one-way here.

class AttendanceSummary(Base):
    __tablename__ = "attendance_summaries"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    
    total_classes = Column(Integer, default=0)
    attended_classes = Column(Integer, default=0)
    absent_classes = Column(Integer, default=0)
    attendance_percentage = Column(Float, default=0.0)
    status = Column(String, default="eligible") # eligible, barred