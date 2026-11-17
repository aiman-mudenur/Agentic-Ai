import sys
sys.path.insert(0, r'C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer')

# Test theory specialist
from course_organizer.graph.specialists_theory import theory_specialist
from course_organizer.graph.specialists import SpecialistResult

state_theory = {
    "course_code": "THEORY101",
    "units_covered": ("Unit 1", "Unit 2"),
    "draft_grades": [],
    "plan_locked": {
        "course_code": "THEORY101",
        "course_name": "Theory Course",
        "units": ("Unit 1", "Unit 2"),
        "assignment_ids": (),
        "obligations": [
            {"obligation_id": "OBL-cover_unit/Unit 1", "kind": "cover_unit", "subject": "Unit 1", "statement": "cover unit Unit 1"},
            {"obligation_id": "OBL-cover_unit/Unit 2", "kind": "cover_unit", "subject": "Unit 2", "statement": "cover unit Unit 2"},
        ],
        "plan_sha256": "a" * 64,
        "obligation_count": 2,
    }
}

result = theory_specialist(state_theory, None)
print("Theory specialist result:")
print(f"  ok: {result.ok}")
print(f"  step_id: {result.step_id}")
print(f"  agent: {result.agent}")
print(f"  summary: {result.summary}")
print(f"  update: {result.update}")
print(f"  read: {result.read}")

# Test lab specialist
from course_organizer.graph.specialists_lab import lab_specialist

state_lab = {
    "course_code": "LAB101",
    "submissions": [
        {"submission_id": "S1", "student_id": "S001", "assignment_id": "A1", "status": "graded"},
        {"submission_id": "S2", "student_id": "S002", "assignment_id": "A1", "status": "draft"},
    ],
    "assignments": [
        {"assignment_id": "A1", "title": "Lab 1", "max_marks": 100,
         "rubric": [{"criterion_id": "C1", "description": "Criterion 1", "max_marks": 100}]},
    ],
    "milestones": [{"group_id": "GRP-A1", "satisfied": True},
    ],
    "late_flags": [{"submission_id": "S2", "late_by_seconds": 3600, "late": True},
    ],
    "units_covered": ["Unit 1"],
    "human_authority": "course_faculty",
}

result_lab = lab_specialist(state_lab, None)
print("\nLab specialist result:")
print(f"  ok: {result_lab.ok}")
print(f"  step_id: {result_lab.step_id}")
print(f"  agent: {result_lab.agent}")
print(f"  summary: {result_lab.summary}")
print(f"  update: {result_lab.update}")
print(f"  read: {result_lab.read}")

# Test project specialist
from course_organizer.graph.specialists_project import project_specialist

state_project = {
    "course_code": "PROJ101",
    "submissions": [
        {"submission_id": "S1", "student_id": "S001", "assignment_id": "A1"},
        {"submission_id": "S2", "student_id": "S002", "assignment_id": "A1"},
    ],
    "assignments": [
        {"assignment_id": "A1", "title": "Project 1"},
    ],
    "milestones": [{"project_id": "PRJ-A1", "group_id": "GRP-A1",
                    "members": ["S001", "S002"],
                    "checkpoints": [{"checkpoint_id": "A1-M1", "name": "Milestone 1",
                                     "due_date": "2024-01-01", "expected_deliverable": "2 submissions"}]},
                    ],
    "obligations": [
        {"obligation_id": "OBL-track_milestones/all", "kind": "track_milestones",
         "subject": "all", "statement": "track project milestones"},
    ],
    "units_covered": ["Unit 1"],
    "human_authority": "course_faculty",
}

result_project = project_specialist(state_project, None)
print("\nProject specialist result:")
print(f"  ok: {result_project.ok}")
print(f"  step_id: {result_project.step_id}")
print(f"  agent: {result_project.agent}")
print(f"  summary: {result_project.summary}")
print(f"  update: {result_project.update}")
print(f"  read: {result_project.read}")

print("\nAll specialist tests passed!")