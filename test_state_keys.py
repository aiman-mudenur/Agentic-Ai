from course_organizer.graph.runner import run_pipeline

run = run_pipeline(course_code="CS201", run_id="fresh_test_audit2", domain="theory")
# Get all state keys containing 'audit' or 'specialist'
audit_keys = [k for k in run.state.keys() if 'audit' in k.lower() or 'specialist' in k.lower()]
print(f"Audit/specialist keys: {audit_keys}")
for k in audit_keys:
    print(f"  {k}: {run.state[k]}")
print()
# Print all state keys that have interesting values
for k, v in run.state.items():
    if any(x in k.lower() for x in ['audit', 'spec', 'domain', 'ok', 'error']):
        print(f"  {k}: {v}")