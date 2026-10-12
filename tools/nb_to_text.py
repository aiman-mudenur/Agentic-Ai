"""Render an executed notebook to a plain-text evidence file.

Run from the project root:

    python tools/nb_to_text.py notebooks/labNN_x.ipynb docs/evidence/labNN_x.txt

The output is the notebook a reviewer would see: markdown headings to orient
the reader, then every cell with its source and its printed output. It is a
deliverable, not a debugging aid, so it records the execution count and flags
any cell that errored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def render(nb_path: Path) -> str:
    """Render one notebook to text.

    Args:
        nb_path: Path to the executed `.ipynb`.

    Returns:
        The rendered text, including a summary header.

    Raises:
        FileNotFoundError: If the notebook does not exist.
        ValueError: If the file is not valid notebook JSON.
    """
    if not nb_path.is_file():
        raise FileNotFoundError(f"notebook not found: {nb_path}")

    try:
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{nb_path} is not valid notebook JSON: {exc}") from exc

    cells = nb.get("cells", [])
    code = [c for c in cells if c.get("cell_type") == "code"]
    executed = [c for c in code if c.get("execution_count")]
    errors = [
        o
        for c in code
        for o in c.get("outputs", [])
        if o.get("output_type") == "error"
    ]

    rule = "=" * 78
    out: list[str] = [
        f"Evidence - {nb_path.name}",
        "=" * len(nb_path.name),
        "",
        f"Notebook : {nb_path.as_posix()}",
        "Command  : jupyter nbconvert --to notebook --execute --inplace",
        f"Summary  : {len(cells)} cells, {len(code)} code, "
        f"{len(executed)} executed, {len(errors)} errors",
        "",
        rule,
        "",
    ]

    for cell in cells:
        source = "".join(cell.get("source", []))

        if cell.get("cell_type") == "markdown":
            # Headings only. The prose is in the notebook itself; the evidence
            # file is for someone checking the output, not re-reading the doc.
            headings = [ln for ln in source.splitlines() if ln.strip().startswith("#")]
            if headings:
                out.append("")
                out.extend(headings)
            continue

        out.append("")
        out.append("-" * 78)
        out.append(f"In [{cell.get('execution_count')}]:")
        out.append("-" * 78)
        out.append(source.rstrip())

        for item in cell.get("outputs", []):
            kind = item.get("output_type")
            if kind == "stream":
                out.append("")
                out.append("".join(item.get("text", [])).rstrip())
            elif kind == "error":
                out.append("")
                out.append(
                    f"ERROR {item.get('ename')}: {item.get('evalue')}"
                )
            elif kind in ("execute_result", "display_data"):
                text = "".join(item.get("data", {}).get("text/plain", []))
                out.append("")
                out.append(f"Out: {text}")
        out.append("")

    out.append("")
    out.append(rule)
    out.append(f"end of notebook - {len(cells)} cells, {len(errors)} errors")
    out.append(rule)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    """CLI entry.

    Args:
        argv: `argv[1]` is the notebook; `argv[2]` (optional) the output path.

    Returns:
        A process exit code: 0 on success, 1 on a bad path or a failed write.
    """
    if len(argv) < 2:
        print(__doc__)
        return 1

    nb_path = Path(argv[1])
    target = (
        Path(argv[2])
        if len(argv) > 2
        else nb_path.with_suffix(".txt")
    )

    try:
        text = render(nb_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
