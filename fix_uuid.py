from pathlib import Path
p = Path('C:/Users/ummea/OneDrive/Documents/Default Project/agentic-course-organizer/.venv/Lib/site-packages/uuid_utils/compat.py')
p.write_text('from . import uuid7 as uuid7\n__all__ = ["uuid7"]\n')
print('Created compat.py')