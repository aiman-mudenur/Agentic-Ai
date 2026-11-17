"""Agent repository: live inventory of agents, tools, skills and framework.

Counts are computed from the running code (registries, module attributes,
installed package versions) — never hardcoded — so the Agent Activity page
always reports what is actually implemented.
"""

from __future__ import annotations

from typing import Any


def _guarded_import(path: str) -> Any | None:
    """Import ``module:attr`` or return None when unavailable."""
    try:
        mod_name, attr = path.split(":")
        mod = __import__(mod_name, fromlist=[attr])
        return getattr(mod, attr, None)
    except Exception:
        return None


def specialist_agents() -> list[dict[str, str]]:
    """The six Phase-A specialists wired into the Lab 7/8 graphs."""
    names = [
        ("notes_repository", "Notes Repository Agent — one current, versioned set of materials."),
        ("assignment_distribution", "Assignment Distribution Agent — releases briefs + rubrics on schedule."),
        ("submission_collection", "Submission Collection Agent — gathers + timestamps submissions."),
        ("similarity", "Plagiarism/Similarity Agent — trigram-Jaccard score, flag-only."),
        ("grading", "Grading Assistance Agent — draft grade + feedback, never publishes."),
        ("milestones", "Project Milestone Tracking Agent — checkpoint status from evidence."),
    ]
    out = []
    for fn_name, summary in names:
        fn = _guarded_import(f"course_organizer.graph.specialists:{fn_name}")
        out.append({"name": fn_name, "summary": summary,
                    "wired": bool(callable(fn)), "source": "graph/specialists.py"})
    return out


def lab_agents() -> list[dict[str, str]]:
    """One agent class per foundations lab (Labs 1-4)."""
    names = [
        ("course_organizer.labs.lab01_planner_loop:PlannerAgent", "Lab 1 planner (clarify → plan → revise)."),
        ("course_organizer.labs.lab02_tool_using_agent:ToolUsingAgent", "Lab 2 tool user (reads + computes via tools)."),
        ("course_organizer.labs.lab03_skill_creation:SkillAgent", "Lab 3 skill invoker (plan + format skills)."),
        ("course_organizer.labs.lab04_memory_retrieval:RetrievalAgent", "Lab 4 retriever (tags + similarity)."),
    ]
    out = []
    for path, summary in names:
        cls = _guarded_import(path)
        out.append({"name": path.split(":")[1], "summary": summary,
                    "wired": cls is not None, "source": path.split(":")[0] + ".py"})
    return out


def tool_inventory() -> list[dict[str, Any]]:
    """Union of the Lab 2 (3 tools) and Lab 4 (4 tools) registries, built live."""
    build_default = _guarded_import("course_organizer.tools.compute:build_default_registry")
    build_lab04 = _guarded_import("course_organizer.tools.compute:build_lab04_registry")
    specs: dict[str, Any] = {}
    for builder in (build_default, build_lab04):
        if builder is None:
            continue
        try:
            reg = builder("inventory-probe")
            for name in reg.names():
                specs.setdefault(name, reg.get(name))
        except Exception:
            continue
    out = []
    for name in sorted(specs):
        spec = specs[name]
        out.append({
            "name": name,
            "summary": getattr(spec, "summary", ""),
            "doc_ref": getattr(spec, "doc_ref", ""),
            "read_only": bool(getattr(spec, "read_only", True)),
        })
    return out


def skill_inventory() -> list[dict[str, str]]:
    """The Lab 3 skills (plan + format), read off their specs."""
    out = []
    for path in ("course_organizer.skills.plan_skill:PLAN_SKILL_SPEC",
                 "course_organizer.skills.format_skill:FORMAT_SKILL_SPEC"):
        spec = _guarded_import(path)
        if spec is None:
            continue
        name = str(getattr(spec, "name", path.split(":")[1]))
        out.append({"name": name,
                    "summary": str(getattr(spec, "summary", getattr(spec, "description", ""))),
                    "source": path.split(":")[0] + ".py"})
    return out


def framework_inventory() -> dict[str, Any]:
    """LangChain/LangGraph (+ MCP): installed versions and where each piece is used.

    Versions come from importlib.metadata; the usage map cites files verified
    against the source tree.
    """
    from importlib import metadata as _metadata

    versions = {}
    for dist in ("langchain", "langchain-core", "langgraph", "langchain-google-genai",
                 "langchain-mcp-adapters", "mcp"):
        try:
            versions[dist] = _metadata.version(dist)
        except Exception:
            versions[dist] = None
    return {
        "versions": versions,
        "uses": [
            {"piece": "StateGraph / END / START", "what": "Lab 7 pipeline + Lab 8 swarm topology",
             "files": ["graph/builder.py", "graph/swarm.py"]},
            {"piece": "Send (fan-out)", "what": "Parallel per-unit/per-submission workers",
             "files": ["graph/swarm.py"]},
            {"piece": "interrupt + Command(resume=…)", "what": "Real human gate; resume without re-running pre-gate nodes",
             "files": ["graph/nodes.py", "graph/runner.py", "graph/swarm.py"]},
            {"piece": "SqliteSaver / InMemorySaver + JsonPlusSerializer", "what": "Durable checkpoints (Lab 6 crash/resume, Lab 7/8 runs)",
             "files": ["graph/runner.py", "graph/builder.py", "runtime/checkpoint.py"]},
            {"piece": "ChatGoogleGenerativeAI (langchain-google-genai)", "what": "Live LLM backend behind the LLMClient protocol",
             "files": ["llm.py"]},
            {"piece": "MultiServerMCPClient / load_mcp_tools", "what": "Real MCP stdio subprocess client (Lab 5)",
             "files": ["connector/client.py"]},
            {"piece": "StructuredTool export", "what": "Lab tools consumable as LangChain tools",
             "files": ["tools/base.py", "memory/base.py"]},
        ],
        "llm_backends": ["scripted (ScriptedLLM — deterministic, offline default)",
                         "live (GeminiLLM — same protocol, needs GOOGLE_API_KEY)"],
        "swarm_merge_strategies": ["generous", "strict", "sparse"],
    }


def full_inventory() -> dict[str, Any]:
    specialists = specialist_agents()
    labs = lab_agents()
    tools = tool_inventory()
    skills = skill_inventory()
    return {
        "counts": {
            "specialist_agents": sum(1 for a in specialists if a["wired"]),
            "lab_agents": sum(1 for a in labs if a["wired"]),
            "agents_total": sum(1 for a in specialists + labs if a["wired"]),
            "tools": len(tools),
            "skills": len(skills),
        },
        "specialist_agents": specialists,
        "lab_agents": labs,
        "tools": tools,
        "skills": skills,
        "framework": framework_inventory(),
    }
