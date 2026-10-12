from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field

from course_organizer.llm import ScriptedLLM, get_llm


class GradeVerdict(BaseModel):
    """Structured grade produced by the grading agent (Lab 2 tool-using agent pattern)."""

    awarded_marks: float = Field(ge=0)
    max_marks: float = Field(ge=0)
    feedback: str = ""
    similarity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    similarity_flagged: bool = False


def _tokens(text: str) -> set[str]:
    return set(w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2)


def extract_text(file_path: str | None, file_name: str | None = None) -> str:
    """Best-effort text extraction (txt/md direct, docx via zip XML, pdf via pypdf)."""
    if not file_path:
        return ""
    p = Path(file_path)
    if not p.is_file():
        return ""
    name = (file_name or p.name).lower()
    try:
        if name.endswith(".docx"):
            try:
                with zipfile.ZipFile(p) as z:
                    xml = z.read("word/document.xml")
                root = ET.fromstring(xml)
                texts = [e.text for e in root.iter() if e.tag.endswith("}t") and e.text]
                if texts:
                    return "\n".join(texts)[:20000]
            except Exception:
                pass
        if name.endswith(".pdf"):
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(p))
                out = []
                for page in reader.pages[:20]:
                    out.append(page.extract_text() or "")
                txt = "\n".join(out).strip()
                if txt:
                    return txt[:20000]
            except Exception:
                pass
        raw = p.read_bytes()
        try:
            return raw.decode("utf-8", errors="ignore")[:20000]
        except Exception:
            return ""
    except Exception:
        return ""


def similarity_vs_others(text: str, other_texts: list[str]) -> float:
    """Max token-overlap ratio of this text vs other submissions (plagiarism hint)."""
    base = _tokens(text)
    if not base:
        return 0.0
    best = 0.0
    for o in other_texts:
        toks = _tokens(o)
        if not toks:
            continue
        overlap = len(base & toks) / max(len(base | toks), 1)
        best = max(best, overlap)
    return round(best, 3)


@ScriptedLLM.builder_for(GradeVerdict)
def _build_grade(payload: dict, llm: ScriptedLLM) -> GradeVerdict:
    """Deterministic offline grader: rubric-keyword coverage + length signal.

    This is the same Lab 2 pattern (tool-using agent -> structured schema):
    the agent 'reads' the submission text (tool) and must return a GradeVerdict.
    Honest heuristic, clearly labelled as agent draft for faculty review.
    """
    max_marks = float(payload.get("max_marks", 20) or 20)
    rubric = str(payload.get("rubric_text", "") or "")
    description = str(payload.get("description", "") or "")
    text = str(payload.get("submission_text", "") or "")
    similarity = float(payload.get("similarity_score", 0.0) or 0.0)

    rubric_keys = _tokens(rubric) | _tokens(description)
    sub_toks = _tokens(text)
    if rubric_keys:
        coverage = len(rubric_keys & sub_toks) / len(rubric_keys)
    else:
        coverage = min(len(sub_toks) / 200.0, 1.0)
    length_factor = min(len(text) / 3000.0, 1.0)
    score_ratio = round(0.65 * coverage + 0.35 * length_factor, 3)
    score_ratio = max(0.0, min(score_ratio, 1.0))
    if len(text.strip()) < 50:
        score_ratio = min(score_ratio, 0.2)
    awarded = round(score_ratio * max_marks, 1)
    flagged = similarity >= 0.8
    fb = (
        f"Agent draft grade (offline heuristic): rubric-keyword coverage {coverage:.0%}, "
        f"length signal {length_factor:.0%}. Faculty review recommended."
    )
    return GradeVerdict(
        awarded_marks=awarded,
        max_marks=max_marks,
        feedback=fb,
        similarity_score=similarity,
        similarity_flagged=flagged,
    )


def grade_with_agent(
    *,
    submission_text: str,
    max_marks: float,
    rubric_text: str | None,
    description: str | None,
    similarity_score: float = 0.0,
) -> GradeVerdict:
    """Run the grading agent (scripted backend offline; live when LLM_BACKEND=live)."""
    import json

    llm = get_llm()
    payload = json.dumps(
        {
            "submission_text": submission_text[:8000],
            "max_marks": max_marks,
            "rubric_text": rubric_text or "",
            "description": description or "",
            "similarity_score": similarity_score,
        }
    )
    return llm.generate_structured(
        system="You are a fair course grading assistant. Return only schema-valid grades.",
        user=payload,
        schema=GradeVerdict,
        step="auto_grade",
    )
