from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routers import auth, faculty, student, hod, courses, assignments, submissions, notes, releases, metrics, runs, google, classroom, dev, projects, plans, openclaw
from db.init_db import init_db

app = FastAPI(title="Course Organizer API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.on_event("startup")
def on_startup():
    init_db()

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(google.router, prefix="/google", tags=["google"])
app.include_router(classroom.router, prefix="/classroom", tags=["classroom"])
app.include_router(courses.router, prefix="/courses", tags=["courses"])
app.include_router(notes.router, prefix="/notes", tags=["notes"])
app.include_router(assignments.router, prefix="/assignments", tags=["assignments"])
app.include_router(submissions.router, prefix="/submissions", tags=["submissions"])
app.include_router(faculty.router, prefix="/faculty", tags=["faculty"])
app.include_router(student.router, prefix="/student", tags=["student"])
app.include_router(hod.router, prefix="/hod", tags=["hod"])
app.include_router(dev.router, prefix="/dev", tags=["dev"])
app.include_router(runs.router, prefix="/runs", tags=["runs"])
app.include_router(releases.router, prefix="/releases", tags=["releases"])
app.include_router(metrics.router, prefix="/metrics", tags=["metrics"])
app.include_router(projects.router, prefix="/projects", tags=["projects"])
app.include_router(plans.router, prefix="/plans", tags=["plans"])
# Optional OpenClaw AI Course Assistant layer (disabled unless configured).
app.include_router(openclaw.router, prefix="/openclaw", tags=["openclaw"])
