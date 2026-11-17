with open('src/course_organizer/graph/nodes.py', 'r') as f:
    content = f.read()
print(f'registry occurrences: {content.count("registry")}')
print(f'domain_error occurrences: {content.count("domain_error")}')
print(f'try: occurrences: {content.count("try:")}')
print(f'except Exception: occurrences: {content.count("except Exception")}')