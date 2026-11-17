from course_organizer.graph.runner import GraphRun
import inspect

# Find the _absorb method
src = inspect.getsource(GraphRun._absorb)
print(src)