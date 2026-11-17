"""Lab 1 planner page — clarify -> plan -> revise loop in the Streamlit UI."""

import logging
import sys
from pathlib import Path

import streamlit as st

# Bootstrap paths: ui_roles/ for shared helpers, src/ for course_organizer labs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_helpers import topbar
topbar()
from ui_helpers import API, token_headers
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from course_organizer.labs.lab01_planner_loop import (
    MAX_REVISIONS,
    PlannerAgent,
    PlannerResult,
    run_planner_loop,
)
from course_organizer.logging_setup import log_event
from course_organizer.schemas import ClarifiedRequest, ClarifyingQuestion, CourseWorkspacePlan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Streamlit-compatible feedback provider
# ---------------------------------------------------------------------------


class StreamlitFeedback:
    """A FeedbackProvider that returns answers from a dict.

    In the UI the user edits answers in text inputs; this provider simply
    returns whatever has been recorded so far, falling back to the question's
    example value when nothing has been entered yet.
    """

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}

    def answer_question(self, question: ClarifyingQuestion) -> str:
        if question.field in self.answers and self.answers[question.field].strip():
            return self.answers[question.field].strip()
        return question.example

    def wants_changes(self, plan: CourseWorkspacePlan) -> bool:
        return False

    def get_feedback(self, plan: CourseWorkspacePlan) -> str:
        return ""


# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------


def _reset_planner_state() -> None:
    """Clear all planner session state."""
    for key in (
        "planner_state",
        "planner_request",
        "planner_agent",
        "planner_clarified",
        "planner_questions",
        "planner_answers",
        "planner_plan",
        "planner_revision_count",
        "planner_feedback",
        "planner_one_shot",
    ):
        st.session_state.pop(key, None)


def _init_state() -> None:
    if "planner_state" not in st.session_state:
        st.session_state["planner_state"] = "request"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("Course Planner")
st.caption("Clarify a request, propose an editable course workspace plan, and revise it with feedback.")

if st.session_state.get("role") not in ("faculty", "hod", "admin") or not st.session_state.get("token"):
    st.warning("Login as Faculty, HoD, or Admin first (Login page).")
    st.stop()

_init_state()
state = st.session_state.get("planner_state", "request")

# --- State: request ---------------------------------------------------------
if state == "request":
    st.subheader("Step 1 — Describe your request")
    st.caption("Enter a free-text description of the course workspace you want to organise.")
    request_text = st.text_area(
        "Course organization request",
        placeholder="e.g. Organise my course",
        key="planner_request_input",
        height=120,
    )
    if st.button("Submit Request", key="planner_submit_request"):
        if not (request_text or "").strip():
            st.error("Please enter a request.")
        else:
            try:
                agent = PlannerAgent()
                provider = StreamlitFeedback({})
                clarified = agent.clarify(request_text.strip(), provider)
                st.session_state["planner_request"] = request_text.strip()
                st.session_state["planner_agent"] = agent
                st.session_state["planner_clarified"] = clarified
                st.session_state["planner_questions"] = list(clarified.questions_asked)
                st.session_state["planner_answers"] = {
                    q.field: (q.answer or q.example) for q in clarified.questions_asked
                }
                st.session_state["planner_revision_count"] = 0
                log_event(logger, "planner_request_submitted", request=request_text.strip()[:200])
                if clarified.questions_asked:
                    st.session_state["planner_state"] = "clarifying"
                else:
                    # No questions needed — go straight to plan.
                    plan = agent.propose_plan(clarified)
                    st.session_state["planner_plan"] = plan
                    st.session_state["planner_state"] = "proposed"
                st.rerun()
            except Exception as e:
                st.error(str(e)[:500])

# --- State: clarifying ------------------------------------------------------
elif state == "clarifying":
    request = st.session_state.get("planner_request", "")
    st.subheader("Step 2 — Clarifying questions")
    st.caption(f"Request: {request}")
    questions = st.session_state.get("planner_questions", [])
    answers = st.session_state.get("planner_answers", {})

    if questions:
        st.write("The planner needs a few details before it can propose a plan.")
        for q in questions:
            st.text_input(
                q.question,
                value=answers.get(q.field, q.example or ""),
                key=f"planner_ans_{q.field}",
                help=q.why_it_matters,
            )
            st.caption(f"_Why it matters:_ {q.why_it_matters}  |  _Example:_ {q.example}")
    else:
        st.info("No clarifying questions needed — all parameters were provided.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Generate Plan", key="planner_generate"):
            try:
                agent = st.session_state["planner_agent"]
                # Collect edited answers from session state.
                edited = {}
                for q in questions:
                    edited[q.field] = st.session_state.get(f"planner_ans_{q.field}", q.answer or q.example)
                provider = StreamlitFeedback(edited)
                clarified = agent.clarify(request, provider)
                plan = agent.propose_plan(clarified)
                st.session_state["planner_clarified"] = clarified
                st.session_state["planner_questions"] = list(clarified.questions_asked)
                st.session_state["planner_answers"] = {
                    q.field: (q.answer or q.example) for q in clarified.questions_asked
                }
                st.session_state["planner_plan"] = plan
                st.session_state["planner_revision_count"] = 0
                log_event(logger, "planner_plan_proposed", plan_id=plan.plan_id, steps=len(plan.steps))
                st.session_state["planner_state"] = "proposed"
                st.rerun()
            except Exception as e:
                st.error(str(e)[:500])
    with col2:
        if st.button("Cancel", key="planner_cancel_clarify"):
            log_event(logger, "planner_cancelled", stage="clarify")
            _reset_planner_state()
            st.rerun()

# --- State: proposed -------------------------------------------------------
elif state == "proposed":
    plan = st.session_state.get("planner_plan")
    clarified = st.session_state.get("planner_clarified")
    if plan is None:
        st.error("No plan available. Please start over.")
        if st.button("Start New Plan", key="planner_restart_proposed"):
            _reset_planner_state()
            st.rerun()
        st.stop()

    st.subheader("Step 3 - Review the proposed plan")
    if clarified is not None:
        st.caption(f"Scope: {clarified.summary()}")
    st.caption(f"Plan ID: {plan.plan_id}  |  Status: {plan.status.value.upper()}  |  Hash: {plan.plan_sha256()[:16]}...  |  Revisions: {st.session_state.get('planner_revision_count', 0)}")

    if plan.open_questions:
        st.warning("Open questions: " + ", ".join(plan.open_questions))

    st.code(plan.outline(), language="text")

    # Auto-save plan to backend on first render of proposed state.
    if "planner_saved" not in st.session_state:
        try:
            r = requests.post(
                f"{API}/plans/",
                headers=token_headers(),
                params={
                    "plan_id": plan.plan_id,
                    "plan_hash": plan.plan_sha256(),
                    "course_code": clarified.course_code if clarified else "UNSPECIFIED",
                    "content": plan.outline(),
                    "open_questions": ", ".join(plan.open_questions),
                    "revision_count": st.session_state.get("planner_revision_count", 0),
                },
                timeout=10,
            )
            if r.ok:
                st.session_state["planner_saved"] = True
                st.session_state["planner_backend_status"] = "draft"
                log_event(logger, "planner_saved_to_backend", plan_id=plan.plan_id)
            else:
                st.session_state["planner_saved"] = False
                st.session_state["planner_backend_status"] = "unsaved"
                try:
                    st.error(r.json().get("detail", r.text[:300]))
                except Exception:
                    st.error(f"{r.status_code}: {r.text[:300]}")
        except Exception as e:
            st.session_state["planner_saved"] = False
            st.session_state["planner_backend_status"] = "unsaved"
            st.error(str(e)[:300])

    backend_status = st.session_state.get("planner_backend_status", "unsaved")

    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("Accept Plan", key="planner_accept"):
            log_event(logger, "planner_accepted", plan_id=plan.plan_id, revisions=st.session_state.get("planner_revision_count", 0))
            st.session_state["planner_state"] = "accepted"
            st.rerun()
    with col2:
        if st.button("Revise Plan", key="planner_revise"):
            st.session_state["planner_state"] = "revising"
            st.rerun()
    with col3:
        if backend_status == "draft":
            if st.button("Approve Plan", key="planner_approve"):
                try:
                    r = requests.post(
                        f"{API}/plans/{plan.plan_id}/approve",
                        headers=token_headers(),
                        params={"plan_hash": plan.plan_sha256()},
                        timeout=10,
                    )
                    if r.ok:
                        st.session_state["planner_backend_status"] = "approved"
                        log_event(logger, "planner_approved_backend", plan_id=plan.plan_id)
                        st.success("Plan approved by faculty.")
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])
        elif backend_status == "approved":
            if st.button("Lock Plan", key="planner_lock"):
                try:
                    r = requests.post(
                        f"{API}/plans/{plan.plan_id}/lock",
                        headers=token_headers(),
                        params={"plan_hash": plan.plan_sha256()},
                        timeout=10,
                    )
                    if r.ok:
                        st.session_state["planner_backend_status"] = "locked"
                        log_event(logger, "planner_locked_backend", plan_id=plan.plan_id)
                        st.success("Plan locked.")
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])
        else:
            if st.button("Save to Backend", key="planner_save_backend"):
                try:
                    r = requests.post(
                        f"{API}/plans/",
                        headers=token_headers(),
                        params={
                            "plan_id": plan.plan_id,
                            "plan_hash": plan.plan_sha256(),
                            "course_code": clarified.course_code if clarified else "UNSPECIFIED",
                            "content": plan.outline(),
                            "open_questions": ", ".join(plan.open_questions),
                            "revision_count": st.session_state.get("planner_revision_count", 0),
                        },
                        timeout=10,
                    )
                    if r.ok:
                        st.session_state["planner_saved"] = True
                        st.session_state["planner_backend_status"] = "draft"
                        st.success("Plan saved to backend (status: DRAFT).")
                        log_event(logger, "planner_saved_to_backend", plan_id=plan.plan_id)
                        st.rerun()
                    else:
                        try:
                            st.error(r.json().get("detail", r.text[:300]))
                        except Exception:
                            st.error(f"{r.status_code}: {r.text[:300]}")
                except Exception as e:
                    st.error(str(e)[:300])
    with col4:
        if st.button("Cancel", key="planner_cancel_proposed"):
            log_event(logger, "planner_cancelled", stage="proposed")
            _reset_planner_state()
            st.rerun()

    if backend_status == "draft":
        st.caption("Backend status: DRAFT (saved). Approve when ready.")
    elif backend_status == "approved":
        st.caption("Backend status: APPROVED by faculty. Lock when ready.")
    elif backend_status == "locked":
        st.caption("Backend status: LOCKED.")
    else:
        st.caption("Backend status: not saved.")

# --- State: revising -------------------------------------------------------
elif state == "revising":
    plan = st.session_state.get("planner_plan")
    if plan is None:
        st.error("No plan available. Please start over.")
        if st.button("Start New Plan", key="planner_restart_revising"):
            _reset_planner_state()
            st.rerun()
        st.stop()

    st.subheader("Step 4 — Revise the plan")
    st.caption(f"Plan ID: {plan.plan_id}  |  Status: {plan.status.value.upper()}  |  Revisions so far: {st.session_state.get('planner_revision_count', 0)}")
    st.code(plan.outline(), language="text")

    if plan.open_questions:
        st.warning("Open questions: " + ", ".join(plan.open_questions))

    feedback = st.text_area("Revision feedback", key="planner_feedback_input", height=100,
                            placeholder="e.g. add a step for peer review of drafts")
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Submit Revision", key="planner_submit_revision"):
            if not (feedback or "").strip():
                st.error("Feedback cannot be empty.")
            else:
                try:
                    agent = st.session_state["planner_agent"]
                    updated = agent.revise(plan, feedback.strip())
                    st.session_state["planner_plan"] = updated
                    st.session_state["planner_revision_count"] = st.session_state.get("planner_revision_count", 0) + 1
                    st.session_state["planner_feedback"] = feedback.strip()
                    log_event(logger, "planner_revised", plan_id=updated.plan_id, revision=st.session_state["planner_revision_count"])
                    st.session_state["planner_state"] = "proposed"
                    st.rerun()
                except Exception as e:
                    st.error(str(e)[:500])
    with col2:
        if st.button("Accept Plan", key="planner_accept_from_revise"):
            log_event(logger, "planner_accepted", plan_id=plan.plan_id, revisions=st.session_state.get("planner_revision_count", 0))
            st.session_state["planner_state"] = "accepted"
            st.rerun()
    with col3:
        if st.button("Cancel", key="planner_cancel_revising"):
            log_event(logger, "planner_cancelled", stage="revising")
            _reset_planner_state()
            st.rerun()

# --- State: accepted -------------------------------------------------------
elif state == "accepted":
    plan = st.session_state.get("planner_plan")
    st.subheader("Plan accepted")
    if plan is not None:
        st.success("Plan accepted (status: DRAFT). No consequential actions were triggered.")
        st.code(plan.outline(), language="text")
        st.caption(f"Plan ID: {plan.plan_id} | Hash: {plan.plan_sha256()[:16]}… | Revisions: {len(plan.revisions)}")
        st.info("The plan remains a DRAFT. Locking and publication happen only through the governed execution pipeline (faculty approval required).")
    if st.button("Start New Plan", key="planner_new_accepted"):
        _reset_planner_state()
        st.rerun()

# --- State: cancelled ------------------------------------------------------
elif state == "cancelled":
    st.warning("Planning session cancelled.")
    if st.button("Start New Plan", key="planner_new_cancelled"):
        _reset_planner_state()
        st.rerun()

else:
    st.error(f"Unknown planner state: {state}")
    if st.button("Reset", key="planner_reset_unknown"):
        _reset_planner_state()
        st.rerun()
