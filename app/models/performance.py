from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Text, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base_class import Base
import enum

class ResultStatus(str, enum.Enum):
    PASS = "Pass"
    FAIL = "Fail"
    AT_RISK = "At_Risk"

class SessionalMark(Base):
    __tablename__ = "sessional_marks"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=False)
    
    midterm_marks = Column(Float, default=0.0)
    quiz_marks = Column(Float, default=0.0)
    assignment_marks = Column(Float, default=0.0)
    project_marks = Column(Float, default=0.0)
    total_sessional_marks = Column(Float, default=0.0)
    
    student = relationship("Student", back_populates="sessional_marks")

class FinalMark(Base):
    __tablename__ = "final_marks"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=False)
    
    final_exam_marks = Column(Float, default=0.0)
    total_course_marks = Column(Float, default=0.0)
    grade = Column(String, nullable=True) # "A", "B+"
    result_status = Column(Enum(ResultStatus), nullable=True)
    
    student = relationship("Student", back_populates="final_marks")

class PerformancePrediction(Base):
    __tablename__ = "performance_predicts"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    
    predicted_status = Column(Enum(ResultStatus), nullable=False)
    predicted_score = Column(Float, nullable=False)
    confidence_score = Column(Float, nullable=True)
    model_version = Column(String, nullable=True) # "v1.0"
    
    student = relationship("Student", back_populates="predictions")

class GradeImportLog(Base):
    __tablename__ = "grade_import_logs"
    id = Column(Integer, primary_key=True, index=True)
    lecturer_id = Column(Integer, ForeignKey("lecturers.id"), nullable=False)
    offering_id = Column(Integer, ForeignKey("subject_offerings.id"), nullable=False)
    
    original_filename = Column(String, nullable=False)
    stored_file_path = Column(String, nullable=False)
    status = Column(String, default="processing") # success, failed
    error_details = Column(Text, nullable=True)
    imported_at = Column(DateTime(timezone=True), server_default=func.now())

# ==========================================
# LIVE ATTENDANCE LOGGING
# ==========================================
class AttendanceLog(Base):
    __tablename__ = "attendance_logs"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    timetable_id = Column(Integer, ForeignKey("timetables.id"), nullable=False)
    
    # The unique UUID generated when the lecturer clicked "Start"
    session_id = Column(String, index=True, nullable=False) 
    
    # "Present", "Late", "Excused", etc.
    status = Column(String, default="Present") 
    
    # Automatically records the exact millisecond they scanned
    scan_time = Column(DateTime(timezone=True), server_default=func.now()) 
    
    # Security: Stores the phone's unique ID to prevent scanning for a friend
    device_fingerprint = Column(String, nullable=True) 

# ==========================================
# NEW: GAMIFICATION STATS
# ==========================================
class StudentGamification(Base):
    __tablename__ = "student_gamification"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), unique=True, nullable=False)
    
    xp_points = Column(Integer, default=0)
    current_streak = Column(Integer, default=0)
    
    # We use this to check if a scan is on a "new day" to increment the streak
    last_scan_date = Column(DateTime(timezone=True), nullable=True)
    
# ==========================================
# NEW: DYNAMIC ASSESSMENTS & GRADEBOOK
# ==========================================
class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=False)
    
    name = Column(String, nullable=False) # e.g., "Quiz 1", "Assignment 2", "Midterm"
    category = Column(String, nullable=False) # e.g., "Quiz", "Assignment", "Presentation", "Exam"
    max_marks = Column(Float, nullable=False)
    weightage = Column(Float, nullable=False)
    
    # --- NEW: LMS WORKFLOW COLUMNS ---
    description = Column(Text, nullable=True) # Instructions from lecturer
    deadline = Column(DateTime(timezone=True), nullable=True)
    file_path = Column(String, nullable=True) # Lecturer's uploaded question/resource file
    status = Column(String, default="Active") # "Active", "Closed"
    
class StudentAssessmentRecord(Base):
    __tablename__ = "student_assessment_records"

    id = Column(Integer, primary_key=True, index=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    
    obtained_marks = Column(Float, nullable=True) # Null means it hasn't been graded yet
    
    # --- NEW: LMS WORKFLOW COLUMNS ---
    submitted_file_path = Column(String, nullable=True) # Student's uploaded answer file
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    feedback = Column(Text, nullable=True) # Lecturer's comments
    status = Column(String, default="Pending") # "Pending", "Submitted", "Graded"