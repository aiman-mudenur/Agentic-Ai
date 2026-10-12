from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select, func

from models.assignment import Assignment
from models.submission import Submission
from models.progress import StudentProgress


def recompute_progress(
    session: Session,
    student_id: int,
    course_code: str,
    unit_number: Optional[str] = None,
    assignment_id: Optional[int] = None,
) -> StudentProgress:

    total_q = select(func.count(Assignment.id)).where(
        Assignment.course_code == course_code,
        Assignment.is_published == True,
    )
    if unit_number:
        total_q = total_q.where(Assignment.unit_number == unit_number)
    if assignment_id:
        total_q = total_q.where(Assignment.id == assignment_id)

    submitted_q = (
        select(func.count(Submission.id))
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(
            Submission.student_id == student_id,
            Assignment.course_code == course_code,
        )
    )
    graded_q = (
        select(func.count(Submission.id))
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(
            Submission.student_id == student_id,
            Assignment.course_code == course_code,
            Submission.status == "graded",
        )
    )

    if unit_number:
        submitted_q = submitted_q.where(Assignment.unit_number == unit_number)
        graded_q = graded_q.where(Assignment.unit_number == unit_number)
    if assignment_id:
        submitted_q = submitted_q.where(Assignment.id == assignment_id)
        graded_q = graded_q.where(Assignment.id == assignment_id)

    total = session.exec(total_q).one() or 0
    submitted = session.exec(submitted_q).one() or 0
    graded = session.exec(graded_q).one() or 0

    if total == 0:
        pct = 0.0
    else:
        pct = round((submitted / float(total)) * 100.0, 2)

    if graded > 0 and submitted >= graded:
        status = "graded"
    elif submitted > 0:
        status = "submitted"
    elif pct > 0:
        status = "in_progress"
    else:
        status = "not_started"

    marks_q = (
        select(
            func.coalesce(func.sum(Submission.awarded_marks), 0.0),
            func.coalesce(func.sum(Submission.max_marks), 0.0),
        )
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(
            Submission.student_id == student_id,
            Assignment.course_code == course_code,
            Submission.status == "graded",
            Submission.awarded_marks.is_not(None),
            Submission.max_marks.is_not(None),
        )
    )
    if unit_number:
        marks_q = marks_q.where(Assignment.unit_number == unit_number)
    if assignment_id:
        marks_q = marks_q.where(Assignment.id == assignment_id)

    marks_obt, marks_max = session.exec(marks_q).one()

    prog_q = select(StudentProgress).where(
        StudentProgress.student_id == student_id,
        StudentProgress.course_code == course_code,
    )
    if unit_number:
        prog_q = prog_q.where(StudentProgress.unit_number == unit_number)
    else:
        prog_q = prog_q.where(StudentProgress.unit_number.is_(None))
    if assignment_id:
        prog_q = prog_q.where(StudentProgress.assignment_id == assignment_id)
    else:
        prog_q = prog_q.where(StudentProgress.assignment_id.is_(None))

    prog = session.exec(prog_q).first()
    if not prog:
        prog = StudentProgress(
            student_id=student_id,
            course_code=course_code,
            unit_number=unit_number,
            assignment_id=assignment_id,
        )

    prog.status = status
    prog.completion_pct = pct
    prog.marks_obtained = marks_obt if marks_obt > 0 else None
    prog.max_marks = marks_max if marks_max > 0 else None
    from datetime import timezone
    prog.updated_at = datetime.now(timezone.utc)

    session.add(prog)
    session.commit()
    session.refresh(prog)
    return prog
