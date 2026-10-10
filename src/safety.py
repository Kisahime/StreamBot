from __future__ import annotations

import re

DEFLECT = "nah I'm leaving that one. anyway."

# Requests the persona already refuses. Matched locally so the model is not asked to invent a refusal.
_ASK = re.compile(
    r"(?i)\b("
    r"diagnos(e|is|ing)|what meds|should i take|prescri(be|ption)|dosage|"
    r"legal advice|should i sue|lawsuit|"
    r"who should i vote|political (take|opinion)|vote for|"
    r"real-world politics"
    r")\b"
)

# A short backstop. The model prompt is not the only gate.
_SLUR = re.compile(
    r"(?i)\b("
    r"nigger|nigga|faggot|retard|tranny|kike"
    r")\b"
)


def is_blocked(text: str) -> bool:
    raw = text or ""
    if _SLUR.search(raw):
        return True
    return bool(_ASK.search(raw))
