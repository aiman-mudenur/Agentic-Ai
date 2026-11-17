from course_organizer.graph.runner import run_pipeline

run = run_pipeline(course_code="CS201", run_id="fresh_test_audit3", domain="theory")
# Check document-related keys
doc_keys = [k for k in run.state.keys() if 'document' in k.lower()]
print(f"Document keys: {doc_keys}")
for k in doc_keys:
    v = run.state[k]
    # Truncate long values
    print(f"  {k}: {str(v)[:200] if len(str(v)) > 200 else v}")

print()
# Check drift_findings
print(f"drift_findings: {run.state.get('drift_findings')}")
print(f"validation: {run.state.get('validation')}")
print(f"specialist_ok: {run.state.get('specialist_ok')}")
print(f"domain_error: {run.state.get('domain_error')}")
print(f"nodes_ran: {run.nodes_ran()}")

print()
# Check audit_trail entries for assemble
for entry in run.state.get('audit_trail', []):
    if entry.get('node') == 'assemble':
        print(f"assemble audit: {entry.get('summary')}")