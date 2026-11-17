from course_organizer.skills.base import SkillError

try:
    raise SkillError('unknown domain invalid_domain; available: lab, project, theory')
except Exception as e:
    print(f'Caught: {type(e).__name__}: {e}')
    print('domain_error would be set to True')
    print('specialist_ok would be set to False')