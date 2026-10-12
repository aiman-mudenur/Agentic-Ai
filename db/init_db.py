from sqlmodel import Session, select
from db.engine import engine
from db.base import SQLModel
from models.auth import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

def init_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if not session.exec(select(User).where(User.email == "faculty@gmail.com")).first():
            session.add(User(username="faculty", email="faculty@gmail.com", full_name="Faculty", role="faculty", hashed_password=pwd_context.hash("faculty123".encode('utf-8')[:72].decode('utf-8', errors='ignore')) if False else pwd_context.hash("faculty123")))
        if not session.exec(select(User).where(User.email == "student@gmail.com")).first():
            session.add(User(username="student", email="student@gmail.com", full_name="Student", role="student", hashed_password=pwd_context.hash("student123")))
        if not session.exec(select(User).where(User.email == "admin@gmail.com")).first():
            session.add(User(username="admin", email="admin@gmail.com", full_name="Admin", role="admin", hashed_password=pwd_context.hash("admin123")))
        session.commit()
