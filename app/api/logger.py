from sqlalchemy.orm import Session
from app.models.system import AuditLog

def log_to_db(
    db: Session, 
    user_id: int, 
    action: str, 
    entity_type: str, 
    entity_id: int = 0, 
    old_value: str = None, 
    new_value: str = None
):
    """
    Saves an operation permanently to the database instead of the terminal.
    """
    try:
        new_log = AuditLog(
            changed_by_user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value
        )
        db.add(new_log)
        db.commit()
    except Exception as e:
        db.rollback()
        # Only print to terminal if the database logger itself crashes!
        print(f"CRITICAL LOGGING ERROR: {e}")