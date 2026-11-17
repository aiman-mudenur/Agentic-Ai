from course_organizer.graph.runner import run_pipeline

run = run_pipeline(course_code="CS201", run_id="debug_gt1", domain="theory")
print(f"status: {run.status}")
print(f"awaiting_gate: {run.awaiting_gate}")
print(f"nodes_ran: {run.nodes_ran()}")
print(f"state keys containing 'domain': {[k for k in run.state.keys() if 'domain' in k.lower()]}")
print(f"domain in state: {'domain' in run.state}")
print(f"state.get('domain'): {run.state.get('domain')}")
print(f"specialist_ok: {run.state.get('specialist_ok')}")
print(f"domain_error: {run.state.get('domain_error')}")