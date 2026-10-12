"""Typed tools: the only route from an agent to data.

Document 2.10, Lab 2: *"Give the agent tools it must call to act, rather than
answering from memory."*

This package is structured as a strict boundary, and **Lab 5 moved the bottom
of it**:

    agent  ->  ToolRegistry  ->  ToolSpec.handler  ->  ConnectorClient
                 (ledger)         (typed in / out)      (authorised, audited)
                                                              |
                                                              v
                                                   SourceAdapter
                                                (the only disk access)

Before Lab 5 the chain ended at ``LocalSeedStore`` and the three readers called
it directly, which made this package a second front door to the corpus -- and
doc 2.10 Lab 5 is explicit that there must not be one: *"No agent should touch
a source directly; a bypass defeats governance later."*

Now every read in `readers.py` goes through
:func:`course_organizer.tools.readers.connector`, the same typed, authorised,
ledger-recording client any agent uses. `seed_store` still exists and is still
the only module that opens `data/seed`, but it is now reachable only from
:mod:`course_organizer.connector`, so "all data access goes through the
connector" is a property of the import graph that a test can check with `ast`,
rather than a habit.

| Module | Role |
|--------|------|
| `base` | `ToolSpec`, `ToolRegistry`, `ToolLedger`, `ToolResult`, `ToolError` |
| `readers` | the three document reads, each going through the connector |
| `compute` | `compute_scope_validation` -- the domain calculator/checker |
| `seed_store` | `LocalSeedStore` -- reached only via `course_organizer.connector` |

**`seed_store` is deliberately not re-exported from here.** It used to be, for
the convenience of Lab 2's tests, and that convenience was a bypass-shaped
affordance: `from course_organizer.tools import get_seed_store` is one keystroke
from an agent module silently growing its own private path to the corpus, with no
authorisation and no ledger row. Code that legitimately needs the store -- the
Lab 2 and Lab 4 tests that build a corpus in a temp directory -- now imports it
from :mod:`course_organizer.tools.seed_store` by its real path, which is visibly
a different act from importing a tool.
"""

from course_organizer.tools.base import (
    ToolCall,
    ToolContext,
    ToolError,
    ToolLedger,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from course_organizer.tools.compute import (
    ScopeQuery,
    ScopeValidationResult,
    ValidationCheck,
    build_default_registry,
    build_lab04_registry,
    register_compute_scope_validation,
)
from course_organizer.tools.readers import (
    AssignmentQuery,
    AssignmentReadResult,
    NotesQuery,
    NotesReadResult,
    SubmissionHistoryQuery,
    SubmissionHistoryResult,
    connector,
    register_read_assignment_rubrics,
    register_read_notes_store,
    register_read_submission_records,
    reset_connector,
    use_connector,
)

__all__ = [
    "AssignmentQuery",
    "AssignmentReadResult",
    "NotesQuery",
    "NotesReadResult",
    "ScopeQuery",
    "ScopeValidationResult",
    "SubmissionHistoryQuery",
    "SubmissionHistoryResult",
    "ToolCall",
    "ToolContext",
    "ToolError",
    "ToolLedger",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ValidationCheck",
    "build_default_registry",
    "build_lab04_registry",
    "connector",
    "reset_connector",
    "register_compute_scope_validation",
    "register_read_assignment_rubrics",
    "register_read_notes_store",
    "register_read_submission_records",
    "use_connector",
]
