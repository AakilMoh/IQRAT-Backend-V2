import os
from sqlalchemy import text
from app.db.session import SessionLocal

def promote_to_super_admin():
    db = SessionLocal()
    try:
        print("👑 Promoting remaining admin to Super Admin (Bypassing ORM)...")
        
        # We use Raw SQL to avoid the Mapper errors, and uppercase 'SUPER_ADMIN' 
        # to satisfy PostgreSQL's strict Enum casing rules.
        
        db.execute(text("""
            UPDATE admins 
            SET role_level = 'SUPER_ADMIN', 
                permissions = 'ALL', 
                department_id = NULL
        """))
        
        db.commit()
        print("✅ Success! Your preserved admin is now a Super Admin with all permissions.")

    except Exception as e:
        print(f"❌ Critical Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    promote_to_super_admin()