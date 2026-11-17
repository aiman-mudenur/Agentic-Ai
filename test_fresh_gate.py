from course_organizer.graph.runner import run_pipeline

run = run_pipeline(course_code="CS201", run_id="fresh_gate_test", domain="theory")
print(f"status: {run.status}")
print(f"awaiting_gate: {run.awaiting_gate}")
print(f"nodes_ran: {run.nodes_ran()}")