#!/usr/bin/env python
"""
Explicit demo data seeding command.
Run manually: python -m scripts.seed_demo
NEVER runs automatically on app startup.

Seeds the semester course catalog only (courses + units). No demo user
accounts: everyone signs up with their own email or Sign in with Google.
"""

from __future__ import annotations

from sqlmodel import Session, select

from db.engine import engine
from models.course import Course, Unit


def seed_demo():
    """Seed the course catalog (CS201, CS202 + units). Idempotent."""
    with Session(engine) as session:
        # Create courses
        courses_data = [
            {"course_code": "CS201", "title": "Data Structures & Algorithms", "description": "Fundamental data structures and algorithm design"},
            {"course_code": "CS202", "title": "Database Systems", "description": "Relational databases, SQL, and transaction management"},
        ]
        for c in courses_data:
            if not session.exec(select(Course).where(Course.course_code == c["course_code"])).first():
                session.add(Course(**c))
        
        # Create units for CS201
        units_cs201 = [
            {"course_code": "CS201", "unit_number": "1", "title": "Arrays & Linked Lists", "description": "Linear data structures"},
            {"course_code": "CS201", "unit_number": "2", "title": "Stacks & Queues", "description": "LIFO and FIFO structures"},
            {"course_code": "CS201", "unit_number": "3", "title": "Trees & Graphs", "description": "Hierarchical and network structures"},
        ]
        for u in units_cs201:
            if not session.exec(select(Unit).where(Unit.course_code == u["course_code"], Unit.unit_number == u["unit_number"])).first():
                session.add(Unit(**u))
        
        # Create units for CS202
        units_cs202 = [
            {"course_code": "CS202", "unit_number": "1", "title": "Relational Model", "description": "Tables, keys, normalization"},
            {"course_code": "CS202", "unit_number": "2", "title": "SQL & Queries", "description": "Select, join, aggregate"},
            {"course_code": "CS202", "unit_number": "3", "title": "Transactions & ACID", "description": "Concurrency and recovery"},
        ]
        for u in units_cs202:
            if not session.exec(select(Unit).where(Unit.course_code == u["course_code"], Unit.unit_number == u["unit_number"])).first():
                session.add(Unit(**u))
        
        session.commit()
        print("[OK] Course catalog seeded: CS201, CS202 (+ units).")
        print("No demo user accounts are created. Sign up with your own email,")
        print("or use Sign in with Google on the Login page.")


if __name__ == "__main__":
    seed_demo()