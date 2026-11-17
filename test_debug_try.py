from course_organizer.graph.runner import run_pipeline

# Test unknown domain
run = run_pipeline(course_code="CS201", run_id="debug_unk2", domain="invalid_domain")
print(f"domain_error: {run.state.get('domain_error')}")
print(f"specialist_ok: {run.state.get('specialist_ok')}")
print(f"specialist_error: {run.state.get('specialist_error')}")
sa = run.state.get("specialist_audit", [])
print(f"specialist_audit: {sa}")
at = run.state.get("audit_trail", [])
print(f"audit_trail length: {len(at)}")
for i, entry in enumerate(at):
    print(f"  [{i}] node: {entry.get('node')}, summary: {entry.get('summary')[:60]}")

# Test theory domain
print()
run2 = run_pipeline(course_code="CS201", run_id="debug_theory2", domain="theory")
print(f"Theory domain:")
print(f"domain_error: {run2.state.get('domain_error')}")
print(f"specialist_ok: {run2.state.get('specialist_ok')}")
sa2 = run2.state.get("specialist_audit", [])
print(f"specialist_audit: {sa2}")