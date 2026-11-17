from course_organizer.graph.registry import DomainSpecialistRegistry

registry = DomainSpecialistRegistry()
domain = "invalid_domain"
try:
    fn = registry.get(domain)
    print(f"registry.get({domain!r}) returned: {fn}")
except Exception as e:
    print(f"registry.get({domain!r}) raised: {type(e).__name__}: {e}")