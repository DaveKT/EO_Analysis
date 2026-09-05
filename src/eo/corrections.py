"""Hand corrections to model output, applied when the published artefacts are built.

The run tables in the working store are never edited: they are what the model
wrote, and diffing runs only means something if that stays true. A correction
lives in a JSON file that names the run it applies to, the field, the value the
model gave, the value it should be, and why. It is applied when `analysis.db`
and the flat-file export are built, and the model's value is kept beside the
corrected one, so a reader can see both.

Two rules keep this from becoming a silent patch layer:

- A correction must match the model's current value exactly. If a re-sweep
  changes the label, the stale correction fails the build rather than
  overwriting a fresh answer.
- Corrections are pinned to one run. Building another run applies none of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

FIELD = "primary_topic"
AS_EXTRACTED = "primary_topic_as_extracted"


@dataclass(frozen=True)
class Correction:
    eo_number: int
    from_value: str
    to_value: str
    reason: str


def load_for_run(
    path: Path | None, run_id: int, model: str, prompt_version: str
) -> list[Correction]:
    """Corrections that apply to this run. Empty if there is no file, or if the
    file is for a different run."""
    if path is None or not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("field") != FIELD:
        raise ValueError(f"{path}: corrections for {data.get('field')!r}, not {FIELD}")
    target = data.get("applies_to", {})
    if (target.get("run_id"), target.get("model"), target.get("prompt_version")) != (
        run_id, model, prompt_version
    ):
        return []
    return [
        Correction(int(c["eo_number"]), c["from"], c["to"], c.get("reason", ""))
        for c in data["corrections"]
    ]


def verified(
    corrections: list[Correction], current: dict[int, str]
) -> dict[int, Correction]:
    """Map eo_number -> correction, after checking each one against the value
    the model actually gave. Raises on any mismatch, listing every offender."""
    problems = []
    for c in corrections:
        if c.eo_number not in current:
            problems.append(f"EO {c.eo_number}: not in this run")
        elif current[c.eo_number] != c.from_value:
            problems.append(
                f"EO {c.eo_number}: correction expects {c.from_value!r},"
                f" model wrote {current[c.eo_number]!r}"
            )
    if problems:
        raise ValueError("stale corrections; fix the file or drop them:\n  " + "\n  ".join(problems))
    return {c.eo_number: c for c in corrections}
