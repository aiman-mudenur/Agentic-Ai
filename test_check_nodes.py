with open('src/course_organizer/graph/nodes.py', 'r') as f:
    lines = f.readlines()
# Check lines around the specialist selection code (should be after locked = LockedPlan line)
for i in range(369, 410):
    line = lines[i]
    spaces = len(line) - len(line.lstrip())
    print(f'{i+1}: {spaces} spaces: {line.rstrip()}')