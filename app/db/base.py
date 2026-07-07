from app.db.base_class import Base

# --- 1. USER LAYER (6 Tables) ---
from app.models.users import (
    User, 
    UserDevice, 
    DeviceChangeRequest,
    Student, 
    Lecturer, 
    Admin
)

# --- 2. ACADEMIC LAYER (10 Tables) ---
from app.models.academic import (
    Department, 
    Degree, 
    SessionBatch, 
    Semester, 
    Section, 
    Classroom, 
    Subject, 
    SubjectOffering, 
    Timetable, 
    ClassSession
)

# --- 3. ATTENDANCE & GAMIFICATION LAYER (8 Tables) ---
from app.models.attendance import (
    Attendance, 
    DeviceLog, 
    ExceptionLog, 
    AttendanceSummary,
    Avatar, 
    AvatarMoodLog, 
    Leaderboard
)

# --- 4. PERFORMANCE LAYER (4 Tables) ---
from app.models.performance import (
    SessionalMark, 
    FinalMark, 
    PerformancePrediction,
    GradeImportLog
)

# --- 5. SYSTEM LAYER (3 Tables) ---
from app.models.system import (
    Notification, 
    SysLog, 
    Setting
)