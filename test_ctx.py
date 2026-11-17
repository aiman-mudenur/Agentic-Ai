from course_organizer.graph.runner import run_pipeline
from course_organizer.skills.base import SkillRegistry

# Let's check the skills context
run = run_pipeline(course_code="CS201", run_id="debug_ctx", domain="theory")

# Access the run's context
ctx = run.context
skills = ctx.skills
print(f"skills type: {type(skills)}")
print(f"has get_global_registry: {hasattr(skills, 'get_global_registry')}")

if hasattr(skills, 'get_global_registry'):
    registry_fn = skills.get_global_registry()
    print(f"registry type: {type(registry_fn)}")
    print(f"callable: {callable(registry_fn)}")
    
    # Try getting a known domain
    try:
        fn = registry_fn.get("theory")
        print(f"registry.get('theory') returned: {fn}")
    except Exception as e:
        print(f"registry.get('theory') raised: {type(e).__name__}: {e}")
    
    # Try getting an unknown domain
    try:
        fn = registry_fn.get("invalid_domain")
        print(f"registry.get('invalid_domain') returned: {fn}")
    except Exception as e:
        print(f"registry.get('invalid_domain') raised: {type(e).__name__}: {e}")