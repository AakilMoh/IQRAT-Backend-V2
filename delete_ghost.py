import os
from sqlalchemy import text
from app.db.session import SessionLocal

# The exact email causing the blockage
TARGET_EMAIL = "raqeebaswar510@gmail.com"

def exorcise_ghost_user():
    db = SessionLocal()
    try:
        print(f"🔍 Hunting for ghost record: {TARGET_EMAIL} (Bypassing ORM)...")
        
        # 1. Find the user ID using Raw SQL
        result = db.execute(text("SELECT id FROM users WHERE email = :email"), {"email": TARGET_EMAIL}).fetchone()
        
        if not result:
            print(f"⚠️ '{TARGET_EMAIL}' is not in the database at all!")
            return
            
        user_id = result[0]
        print(f"👻 Ghost found! (User ID: {user_id}). Erasing now...")
        
        # 2. Wipe any lingering fragmented profiles
        db.execute(text("DELETE FROM students WHERE user_id = :uid"), {"uid": user_id})
        db.execute(text("DELETE FROM lecturers WHERE user_id = :uid"), {"uid": user_id})
        db.execute(text("DELETE FROM admins WHERE user_id = :uid"), {"uid": user_id})
        
        # 3. Delete the core user account
        db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
        
        db.commit()
        print(f"✅ Success! '{TARGET_EMAIL}' has been completely wiped from the database.")

    except Exception as e:
        print(f"❌ Critical Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    exorcise_ghost_user()