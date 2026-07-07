from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Date, Time, Float, Text, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base_class import Base

class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    code = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)

class Degree(Base):
    __tablename__ = "degrees"
    id = Column(Integer, primary_key=True, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    name = Column(String, nullable=False)
    code = Column(String, nullable=False)
    duration_years = Column(Integer, default=4)

class SessionBatch(Base):
    __tablename__ = "session_batches"
    id = Column(Integer, primary_key=True, index=True)
    degree_id = Column(Integer, ForeignKey("degrees.id"), nullable=False)
    name = Column(String, nullable=False) # e.g. "2022-2026"
    start_year = Column(Integer, nullable=False)
    end_year = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True)

class Semester(Base):
    __tablename__ = "semesters"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("session_batches.id"), nullable=False)
    semester_no = Column(Integer, nullable=False)
    name = Column(String, nullable=False) # "Fall 2025"
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    is_active = Column(Boolean, default=True)

class Section(Base):
    __tablename__ = "sections"
    id = Column(Integer, primary_key=True, index=True)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=False)
    name = Column(String, nullable=False) # "Section A"
    max_capacity = Column(Integer, default=50)

class Classroom(Base):
    __tablename__ = "classrooms"
    id = Column(Integer, primary_key=True, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False) # <-- NEW
    room_no = Column(String, unique=True, nullable=False)
    building_name = Column(String, nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    capacity = Column(Integer, default=60)

class Subject(Base):
    __tablename__ = "subjects"
    id = Column(Integer, primary_key=True, index=True)
    degree_id = Column(Integer, ForeignKey("degrees.id"), nullable=False)
    semester_no = Column(Integer, nullable=False, server_default="1")
    code = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    credit_hours = Column(Integer, default=3)
    is_active = Column(Boolean, default=True)

class SubjectOffering(Base):
    __tablename__ = "subject_offerings"
    id = Column(Integer, primary_key=True, index=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=False)
    lecturer_id = Column(Integer, ForeignKey("lecturers.id"), nullable=False)
    is_active = Column(Boolean, default=True)
    
    lecturer = relationship("Lecturer", back_populates="subject_offerings")
    timetables = relationship("Timetable", back_populates="offering")

class Timetable(Base):
    __tablename__ = "timetables"
    id = Column(Integer, primary_key=True, index=True)
    offering_id = Column(Integer, ForeignKey("subject_offerings.id"), nullable=False)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)
    day_of_week = Column(String, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    
    offering = relationship("SubjectOffering", back_populates="timetables")
    class_sessions = relationship("ClassSession", back_populates="timetable")

class ClassSession(Base):
    __tablename__ = "class_sessions"
    id = Column(Integer, primary_key=True, index=True)
    timetable_id = Column(Integer, ForeignKey("timetables.id"), nullable=False)
    session_date = Column(Date, nullable=False)
    status = Column(String, default="scheduled") # Can be: scheduled, active, completed
    
    # --- NEW: GEOFENCING & SECURE QR FIELDS ---
    lecturer_latitude = Column(Float, nullable=True)
    lecturer_longitude = Column(Float, nullable=True)
    current_qr_token = Column(String, nullable=True)
    qr_expires_at = Column(DateTime(timezone=True), nullable=True)
    
    timetable = relationship("Timetable", back_populates="class_sessions")
    attendances = relationship("Attendance", back_populates="class_session")
    
class CourseMaterial(Base):
    __tablename__ = "course_materials"
    id = Column(Integer, primary_key=True, index=True)
    offering_id = Column(Integer, ForeignKey("subject_offerings.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    file_size = Column(String, nullable=True) 
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

class Announcement(Base):
    __tablename__ = "announcements"
    id = Column(Integer, primary_key=True, index=True)
    offering_id = Column(Integer, ForeignKey("subject_offerings.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())