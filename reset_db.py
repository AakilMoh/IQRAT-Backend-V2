import os
from sqlalchemy import text
from app.db.session import SessionLocal

def wipe_database():
    db = SessionLocal()
    try:
        print("🧹 Starting full database cleanup (PostgreSQL Truncate)...")

        # 1. Safely find the Super Admin using Raw SQL
        safe_user_id = None
        admin_name = "Unknown"
        
        try:
            admins = db.execute(text("SELECT user_id, full_name, role_level FROM admins")).fetchall()
            for adm in admins:
                if "super" in str(adm[2]).lower():
                    safe_user_id = adm[0]
                    admin_name = adm[1]
                    break
            
            # Fallback if no specific super admin matches
            if not safe_user_id and admins:
                safe_user_id = admins[0][0]
                admin_name = admins[0][1]
        except Exception as e:
            db.rollback()
            print("⚠️ Note: Could not fetch admins cleanly, proceeding to wipe.")

        if safe_user_id:
            print(f"🛡️ Preserving Admin: {admin_name} (User ID: {safe_user_id})")
        else:
            print("⚠️ No Admin found to preserve! Wiping everything...")

        # 2. Get ALL table names from the database dynamically
        tables_result = db.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'")).fetchall()
        
        # Exclude our safe tables
        safe_tables = ['alembic_version', 'users', 'admins', 'departments']
        tables_to_truncate = [t[0] for t in tables_result if t[0] not in safe_tables]

        # 3. Truncate all child tables forcefully with CASCADE
        if tables_to_truncate:
            tables_str = ", ".join(tables_to_truncate)
            print(f"🗑️ Force wiping {len(tables_to_truncate)} tables and their connections via TRUNCATE CASCADE...")
            db.execute(text(f"TRUNCATE TABLE {tables_str} CASCADE;"))
            db.commit()

        # 4. Clean up the Safe Tables manually
        print("🗑️ Deleting Sub-Admins...")
        if safe_user_id:
            db.execute(text("DELETE FROM admins WHERE user_id != :uid"), {"uid": safe_user_id})
            
            # Temporarily un-link the Super Admin from any department so we can delete departments
            db.execute(text("UPDATE admins SET department_id = NULL WHERE user_id = :uid"), {"uid": safe_user_id})
        else:
            db.execute(text("DELETE FROM admins"))
        db.commit()

        print("🗑️ Deleting Departments...")
        try:
            db.execute(text("DELETE FROM departments"))
            db.commit()
        except Exception as e:
            db.rollback()

        print("🗑️ Clearing Base Users...")
        if safe_user_id:
            db.execute(text("DELETE FROM users WHERE id != :uid"), {"uid": safe_user_id})
        else:
            db.execute(text("DELETE FROM users"))
        db.commit()

        print("✅ Database successfully wiped! A fresh start awaits.")

    except Exception as e:
        print(f"❌ Critical Error during cleanup: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    wipe_database()