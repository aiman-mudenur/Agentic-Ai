from course_organizer.graph.runner import run_pipeline

run = run_pipeline(course_code='CS201', run_id='debug3', domain='invalid_domain')
print(f'domain in state: {"domain" in run.state}')
print(f'state.get("domain"): {run.state.get("domain")}')
print(f'specialist_ok: {run.state.get("specialist_ok")}')
print(f'domain_error: {run.state.get("domain_error")}')
print(f'specialist_error: {run.state.get("specialist_error")}')
print(f'specialist_audit: {run.state.get("specialist_audit", [])}')
at = run.state.get('audit_trail', [])
print(f'audit_trail last entry: {at[-1] if at else "N/A"}')