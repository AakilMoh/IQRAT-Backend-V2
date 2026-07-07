from sqlalchemy import Column, Integer, String, Boolean, Enum, ForeignKey, DateTime, Date, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base_class import Base
import enum

# --- ENUMS ---
class UserRole(str, enum.Enum):
    STUDENT = "student"
    LECTURER = "lecturer"
    ADMIN = "admin"

class AdminRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    DEPT_ADMIN = "dept_admin"

class DeviceStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"

class RequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

# --- CORE USER ---
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False)
    is_active = Column(Boolean, default=True)
    
    # --- NEW: Force Password Change Flag ---
    requires_password_change = Column(Boolean, default=True) 
    
    # 2FA & Security
    is_tfa_enabled = Column(Boolean, default=False)
    tfa_secret = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    student_profile = relationship("Student", back_populates="user", uselist=False)
    lecturer_profile = relationship("Lecturer", back_populates="user", uselist=False)
    admin_profile = relationship("Admin", back_populates="user", uselist=False)
    devices = relationship("UserDevice", back_populates="user")
    notifications = relationship("Notification", back_populates="user")
    sys_logs = relationship("SysLog", back_populates="user")

class UserDevice(Base):
    __tablename__ = "user_devices"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    device_fingerprint = Column(String, nullable=False)
    device_name = Column(String, nullable=True)
    status = Column(Enum(DeviceStatus), default=DeviceStatus.ACTIVE)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    
    user = relationship("User", back_populates="devices")

# --- PROFILES ---
class Student(Base):
    __tablename__ = "students"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    full_name = Column(String, index=True)
    reg_no = Column(String, unique=True, index=True, nullable=False) # The Roll No
    
    # Foreign Keys to Academic Layer (Integers)
    degree_id = Column(Integer, ForeignKey("degrees.id"), nullable=True)
    session_id = Column(Integer, ForeignKey("session_batches.id"), nullable=True)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=True)
    
    photo_path = Column(String, nullable=True)
    contact_no = Column(String, nullable=True)
    status = Column(String, default="active")

    # --- NEW: SETTINGS & CUSTOMIZATION ---
    theme_preference = Column(String, default="default") # 'default', 'dark_gold', 'neon_cyber'
    notify_class_reminders = Column(Boolean, default=True) # 15 min before class
    notify_assignment_deadlines = Column(Boolean, default=True)

    # Relationships
    user = relationship("User", back_populates="student_profile")
    device_requests = relationship("DeviceChangeRequest", back_populates="student")
    attendances = relationship("Attendance", back_populates="student")
    avatar = relationship("Avatar", back_populates="student", uselist=False)
    leaderboard_entry = relationship("Leaderboard", back_populates="student")
    
    # Marks relationships
    sessional_marks = relationship("SessionalMark", back_populates="student")
    final_marks = relationship("FinalMark", back_populates="student")
    predictions = relationship("PerformancePrediction", back_populates="student")

class Lecturer(Base):
    __tablename__ = "lecturers"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    
    employee_code = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    designation = Column(String, nullable=True)
    joined_on = Column(Date, nullable=True)

    # --- NEW: PROFILE & SETTINGS COLUMNS ---
    photo_path = Column(String, nullable=True)
    contact_no = Column(String, nullable=True)
    theme_preference = Column(String, default="default") 
    notify_class_reminders = Column(Boolean, default=True) 
    notify_assignment_deadlines = Column(Boolean, default=True)
    
    user = relationship("User", back_populates="lecturer_profile")
    subject_offerings = relationship("SubjectOffering", back_populates="lecturer")

class Admin(Base):
    __tablename__ = "admins"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    admin_id = Column(String, unique=True, index=True, nullable=False) # <-- NEW: Auto-generated ID
    full_name = Column(String, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    
    role_level = Column(Enum(AdminRole), default=AdminRole.DEPT_ADMIN)
    contact_no = Column(String, nullable=True)
    permissions = Column(String, nullable=True) # <-- NEW: e.g. "users,academics,reports"
    
    user = relationship("User", back_populates="admin_profile")

class DeviceChangeRequest(Base):
    __tablename__ = "device_change_requests"
    
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    new_device_fingerprint = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    status = Column(Enum(RequestStatus), default=RequestStatus.PENDING)
    
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    handled_at = Column(DateTime(timezone=True), nullable=True)
    handled_by = Column(Integer, ForeignKey("admins.id"), nullable=True)
    
    student = relationship("Student", back_populates="device_requests")