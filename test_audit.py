from course_organizer.graph.runner import run_pipeline

run = run_pipeline(course_code="CS201", run_id="fresh_test_audit", domain="theory")
print(f"nodes_ran: {run.nodes_ran()}")
print(f"status: {run.status}")
print(f"awaiting_gate: {run.awaiting_gate}")
print(f"specialist_audit: {run.state.get('specialist_audit', [])}")
print(f"domain in state: {'domain' in run.state}")
print(f"specialist_ok: {run.state.get('specialist_ok')}")
print(f"domain_error: {run.state.get('domain_error')}")