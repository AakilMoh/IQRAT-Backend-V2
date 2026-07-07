from sqlalchemy.orm import Session
from app.db.base import Base
from app.db.session import SessionLocal
from app.models.users import User, UserRole, Admin, AdminRole
from app.core.security import get_password_hash

def create_super_admin():
    db = SessionLocal()
    
    email = "project.iqrat@gmail.com" # initial gmail of ours
    password = "admin"  # A stronger password must assigned
    
    # 1. Check if a super user already exists
    user = db.query(User).filter(User.email == email).first()
    if user:
        print("Super User already exists!")
        return

    # 2. Create the User Login (The Keys)
    print("Creating User Credentials...")
    new_user = User(
        email=email,
        hashed_password=get_password_hash(password),
        role=UserRole.ADMIN,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # 3. Create the Admin Profile
    print("Creating Admin Profile...")
    new_admin = Admin(
        user_id=new_user.id,
        role_level=AdminRole.SUPER_ADMIN,
        department_id=None, 
        contact_no="000-0000000"
        # DO NOT add full_name here if it causes a crash
    )
    db.add(new_admin)
    db.commit()
    

    print(f"Super User Created Successfully!")
    print(f"📧 Email: {email}")
    print(f"🔑 Pass : {password}")
    db.close()

if __name__ == "__main__":
    create_super_admin()