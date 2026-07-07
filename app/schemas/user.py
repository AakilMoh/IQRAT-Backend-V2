from pydantic import BaseModel, EmailStr
from typing import Optional

# Shared properties
class UserBase(BaseModel):
    email: EmailStr
    role: str = "student" 

# Properties to receive via API on creation
class UserCreate(UserBase):
    password: str
    full_name: str  # We need this back
    
    # Optional fields (depending on role)
    roll_no: Optional[str] = None       # For Students (Roll No)
    employee_code: Optional[str] = None # For Lecturers/Admins
    designation: Optional[str] = None   # For Lecturers

# Properties to return to client
class User(UserBase):
    id: int
    is_active: bool
    # We will return the specific profile info later
    
    class Config:
        from_attributes = True