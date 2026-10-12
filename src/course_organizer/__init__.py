"""Worklet 2 — Course Project, Notes & Assignment Organizer.

An agentic AI system built through 14 labs (we implement Labs 1-8):
one coordinator plus specialist worker agents, where agents draft and flag
and a named human (course faculty) approves.

The `apniLeap AURA Coordinator` is the orchestrator agent that owns run-state,
the plan, budgets and the audit log.
"""

__version__ = "0.1.0"

# The document names course faculty as the human authority for this worklet
# (Chapter 2, "Human authority: course faculty"). Every gate in this codebase
# resolves to this constant — never to a hardcoded string.
HUMAN_AUTHORITY = "course_faculty"

__all__ = ["HUMAN_AUTHORITY", "__version__"]
