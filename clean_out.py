import shutil, os
out_dir = r'C:\Users\ummea\OneDrive\Documents\Default Project\agentic-course-organizer\out'
if os.path.exists(out_dir):
    shutil.rmtree(out_dir)
    print(f"Cleaned {out_dir}")
else:
    print(f"{out_dir} does not exist")