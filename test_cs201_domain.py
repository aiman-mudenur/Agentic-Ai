from course_organizer.graph.runner import run_pipeline

# Run with CS201 and domain theory
run = run_pipeline(course_code="CS201", run_id="cap_A_theory2", domain="theory")
print(f"status: {run.status}")
print(f"awaiting_gate: {run.awaiting_gate}")
print(f"domain in state: {'domain' in run.state}")
print(f"specialist_audit: {run.state.get('specialist_audit', [])}")
print(f"state keys: {list(run.state.keys())}")

print()
print("---")

# Run with CS201 and domain lab
run2 = run_pipeline(course_code="CS201", run_id="cap_B_lab2", domain="lab")
print(f"status: {run2.status}")
print(f"awaiting_gate: {run2.awaiting_gate}")
print(f"domain in state: {'domain' in run2.state}")
print(f"specialist_audit: {run2.state.get('specialist_audit', [])}")

print()
print("---")

# Run with CS201 and domain project
run3 = run_pipeline(course_code="CS201", run_id="cap_C_proj2", domain="project")
print(f"status: {run3.status}")
print(f"awaiting_gate: {run3.awaiting_gate}")
print(f"domain in state: {'domain' in run3.state}")
print(f"specialist_audit: {run3.state.get('specialist_audit', [])}")