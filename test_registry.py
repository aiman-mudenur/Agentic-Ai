from course_organizer.graph.registry import get_global_registry

registry = get_global_registry()
print(f"registry type: {type(registry)}")
print(f"registry: {registry}")

if hasattr(registry, 'get'):
    print(f"registry.get('theory'): {registry.get('theory')}")
    print(f"registry.get('lab'): {registry.get('lab')}")
    print(f"registry.get('project'): {registry.get('project')}")
    print(f"registry.get('invalid_domain'): {registry.get('invalid_domain')}")