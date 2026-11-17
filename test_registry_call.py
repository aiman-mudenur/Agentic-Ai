from course_organizer.graph.registry import get_global_registry

registry = get_global_registry()
print(f'has __call__: {hasattr(registry, "__call__")}')
print(f'type: {type(registry)}')

# Try calling it
try:
    result = registry()
    print(f'registry() returned: {type(result)}')
    print(f'callable after call: {callable(result)}')
except Exception as e:
    print(f'registry() raised: {type(e).__name__}: {e}')