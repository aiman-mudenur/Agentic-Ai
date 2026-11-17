from course_organizer.graph.runner import run_pipeline

# Run with domain="invalid_domain"
run = run_pipeline(course_code="CS201", run_id="debug_unk", domain="invalid_domain")

print(f"status: {run.status}")
print(f"awaiting_gate: {run.awaiting_gate}")
print(f"nodes_ran: {run.nodes_ran()}")
print(f"domain in state: {'domain' in run.state}")
print(f"state.get('domain'): {run.state.get('domain')}")
print(f"specialist_ok: {run.state.get('specialist_ok')}")
print(f"domain_error: {run.state.get('domain_error')}")
print(f"specialist_error: {run.state.get('specialist_error')}")
print(f"audit_trail length: {len(run.state.get('audit_trail', []))}")
for entry in run.state.get('audit_trail', []):
    print(f"  audit entry node: {entry.get('node')}")

# Check all state keys containing 'ok', 'error', 'spec'
print()
print("=== State keys with ok/error/spec/domain ===")
for k, v in run.state.items():
    lower = k.lower()
    if any(x in lower for x in ['ok', 'error', 'spec', 'domain']):
        print(f"  {k}: {v}")