from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routers import auth, faculty, student, courses, assignments, submissions, notes, runs
from db.init_db import init_db

app = FastAPI(title="Course Organizer API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def on_startup():
    init_db()

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(courses.router, prefix="/courses", tags=["courses"])
app.include_router(notes.router, prefix="/notes", tags=["notes"])
app.include_router(assignments.router, prefix="/assignments", tags=["assignments"])
app.include_router(submissions.router, prefix="/submissions", tags=["submissions"])
app.include_router(faculty.router, prefix="/faculty", tags=["faculty"])
app.include_router(student.router, prefix="/student", tags=["student"])
app.include_router(runs.router, prefix="/runs", tags=["runs"])


@app.get("/health")
def health():
    return {"status": "ok"}
