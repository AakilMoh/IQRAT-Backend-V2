from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base_class import Base
import enum

class NotificationType(str, enum.Enum):
    EMAIL = "email"
    PUSH = "push"
    IN_APP = "in_app"

class SysLog(Base):
    __tablename__ = "sys_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False) # "LOGIN", "DELETE_USER"
    module = Column(String, nullable=True) # "AUTH", "ACADEMIC"
    severity = Column(String, default="info")
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    
    user = relationship("User", back_populates="sys_logs")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    type = Column(Enum(NotificationType), default=NotificationType.IN_APP)
    is_read = Column(Boolean, default=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())
    
    user = relationship("User", back_populates="notifications")

class Setting(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, index=True)
    key_name = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=False)
    category = Column(String, nullable=True) # e.g., "security", "general"
    description = Column(String, nullable=True)
    
class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String, nullable=False) # e.g., "Assessment", "Security"
    entity_id = Column(Integer, nullable=False)  # The ID of the specific item changed
    action = Column(String, nullable=False)      # e.g., "Created Assignment"
    
    old_value = Column(String, nullable=True)    
    new_value = Column(String, nullable=True)    
    
    changed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False) 
    changed_at = Column(DateTime(timezone=True), server_default=func.now())