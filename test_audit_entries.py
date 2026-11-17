from course_organizer.graph.runner import run_pipeline

# Test theory domain
print("=== Theory domain ===")
run = run_pipeline(course_code="CS201", run_id="theory_at", domain="theory")
sa = run.state.get("specialist_audit", [])
print(f"specialist_audit entries: {len(sa)}")
for a in sa:
    print(f"  agent: {a.get('agent')}, ok: {a.get('ok')}")

print()

# Test lab domain
print("=== Lab domain ===")
run2 = run_pipeline(course_code="CS201", run_id="lab_at", domain="lab")
sa2 = run2.state.get("specialist_audit", [])
print(f"specialist_audit entries: {len(sa2)}")
for a in sa2:
    print(f"  agent: {a.get('agent')}, ok: {a.get('ok')}")

print()

# Test project domain
print("=== Project domain ===")
run3 = run_pipeline(course_code="CS201", run_id="proj_at", domain="project")
sa3 = run3.state.get("specialist_audit", [])
print(f"specialist_audit entries: {len(sa3)}")
for a in sa3:
    print(f"  agent: {a.get('agent')}, ok: {a.get('ok')}")

print()

# Check domain in state
print(f"Theory domain in state: {run.state.get('domain')}")
print(f"Lab domain in state: {run2.state.get('domain')}")
print(f"Project domain in state: {run3.state.get('domain')}")