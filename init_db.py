from app.db.session import engine
from app.db.base_class import Base

# Import all models so SQLAlchemy knows they exist
from app.models.users import User, Student, Lecturer, Admin
from app.models.academic import Department, Degree, Semester, Subject, SubjectOffering, Classroom, Timetable
from app.models.performance import SessionalMark, FinalMark, PerformancePrediction, GradeImportLog, AttendanceLog

print("Checking database and creating missing tables...")

# This command tells PostgreSQL to look at all the imported models 
# and safely create any tables that don't exist yet!
Base.metadata.create_all(bind=engine)

print("✅ Tables created successfully! You can start scanning.")