from course_organizer.graph.runner import run_pipeline
from course_organizer.graph.state import CourseState

# Test with domain="invalid_domain" and check full state
run = run_pipeline(course_code="CS201", run_id="debug_extra1", domain="invalid_domain")

# Check ALL state keys related to specialist_ok and domain_error
print("=== Full state keys with 'ok', 'error', 'spec', 'domain' ===")
for k, v in run.state.items():
    lower = k.lower()
    if any(x in lower for x in ['ok', 'error', 'spec', 'domain', 'audit']):
        print(f"  {k}: {v}")

print()
print("=== specialist_audit ===")
sa = run.state.get("specialist_audit", [])
print(f"length: {len(sa)}")
for a in sa:
    print(f"  {a}")

print()
print("=== audit_trail (last few) ===")
at = run.state.get("audit_trail", [])
for entry in at[-3:]:
    print(f"  {entry}")