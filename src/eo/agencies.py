"""Canonical agency names.

The extraction records agencies as the order names them, which is right: the
quote has to match the text. But it means `Secretary of the Treasury` (91 rows),
`Department of the Treasury` (66) and `Treasury` are three entities, so any
"most-tasked agency" ranking is wrong before it starts. 1,146 distinct names
cover 3,195 taskings, and 72% of them appear exactly once.

Three rules govern this module, each of which exists because the obvious
approach is wrong:

**Resolution is by alias lookup, never by splitting on "and".** A row can name
several agencies ("APNSA and APST"), which invites splitting on conjunctions --
but `Department of Health and Human Services` and `Housing and Urban
Development` would not survive it. Instead the cleaned string is scanned for
known aliases longest-first, so multi-word names match before their fragments.

**An unrecognised name becomes its own canonical entity, never a bucket.** Most
of the long tail is real: one-off commissions, task forces and boards that
should appear once and be findable. Folding them into "other" would hide them.

**The raw name is never overwritten.** `agencies_tasked.agency_name` stays
exactly as extracted, because it is what the source quote supports. Canonical
names live beside it, reached through a bridge table, so a disagreement with
this mapping is visible and fixable without re-running anything.

Department vs. Secretary is deliberately merged: the Secretary of Commerce and
the Department of Commerce are one institution for the purpose of counting who
gets tasked. Department of War is merged into Department of Defense for the same
reason -- EO 14347 renamed it in September 2025, and it is one department across
the rename. Nothing is lost by that: the name each order used survives in
agency_mentions.raw_name. See DEFENSE_NOTE.
"""

from __future__ import annotations

import re
import unicodedata

DEPARTMENT = "department"
OFFICE = "office"
OFFICIAL = "official"
COLLECTIVE = "collective"
BODY = "body"

DEFENSE_NOTE = (
    "Executive Order 14347 (2025-09-05) renamed the Department of Defense the"
    " Department of War, for the current administration. Both names resolve to"
    " this one canonical agency, because they are one institution. The rename"
    " is not lost: the name each order actually used is preserved in"
    " agency_mentions.raw_name and agencies_tasked.agency_name, so"
    " WHERE raw_name LIKE '%War%' recovers the Department of War period."
)

# (canonical name, kind, aliases). Aliases are matched against the cleaned form
# of the extracted name -- lowercase, parentheticals removed, leading "the"
# dropped -- and are scanned longest-first so that, for example, "health and
# human services" resolves before "human services" could.
_CANONICAL: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Department of State", DEPARTMENT,
     ("department of state", "secretary of state", "state department")),
    ("Department of the Treasury", DEPARTMENT,
     ("department of the treasury", "secretary of the treasury", "treasury department",
      "treasury")),
    # Department of War is the September 2025 renaming of the same institution
    # (EO 14347), so both names resolve here. See DEFENSE_NOTE for how to
    # recover the distinction, which the raw names preserve.
    ("Department of Defense", DEPARTMENT,
     ("department of defense", "secretary of defense", "defense department", "dod",
      "department of war", "secretary of war")),
    ("Department of Justice", DEPARTMENT,
     ("department of justice", "attorney general", "justice department", "doj")),
    ("Department of the Interior", DEPARTMENT,
     ("department of the interior", "secretary of the interior")),
    ("Department of Agriculture", DEPARTMENT,
     ("department of agriculture", "secretary of agriculture", "usda")),
    ("Department of Commerce", DEPARTMENT,
     ("department of commerce", "secretary of commerce", "commerce department")),
    ("Department of Labor", DEPARTMENT,
     ("department of labor", "secretary of labor", "labor department")),
    ("Department of Health and Human Services", DEPARTMENT,
     ("department of health and human services",
      "secretary of health and human services", "health and human services", "hhs")),
    ("Department of Housing and Urban Development", DEPARTMENT,
     ("department of housing and urban development",
      "secretary of housing and urban development",
      "housing and urban development", "hud")),
    ("Department of Transportation", DEPARTMENT,
     ("department of transportation", "secretary of transportation")),
    ("Department of Energy", DEPARTMENT,
     ("department of energy", "secretary of energy")),
    ("Department of Education", DEPARTMENT,
     ("department of education", "secretary of education")),
    ("Department of Veterans Affairs", DEPARTMENT,
     ("department of veterans affairs", "secretary of veterans affairs",
      "veterans affairs")),
    ("Department of Homeland Security", DEPARTMENT,
     ("department of homeland security", "secretary of homeland security", "dhs")),
    ("Department of the Army", DEPARTMENT,
     ("department of the army", "secretary of the army")),
    ("Department of the Navy", DEPARTMENT,
     ("department of the navy", "secretary of the navy")),
    ("Department of the Air Force", DEPARTMENT,
     ("department of the air force", "secretary of the air force")),

    ("Office of Management and Budget", OFFICE,
     ("office of management and budget", "director of the office of management and budget",
      "director of omb", "omb director", "omb")),
    ("Office of Personnel Management", OFFICE,
     ("office of personnel management", "director of the office of personnel management",
      "director of opm", "opm")),
    ("Office of Science and Technology Policy", OFFICE,
     ("office of science and technology policy", "director of ostp", "ostp")),
    ("Office of the United States Trade Representative", OFFICE,
     ("office of the united states trade representative",
      "united states trade representative", "us trade representative",
      "u s trade representative", "trade representative", "ustr")),
    ("Office of the Director of National Intelligence", OFFICE,
     ("office of the director of national intelligence",
      "director of national intelligence", "odni", "dni")),
    ("Office of National Drug Control Policy", OFFICE,
     ("office of national drug control policy", "ondcp")),
    ("Council on Environmental Quality", OFFICE,
     ("council on environmental quality", "ceq")),
    ("National Security Council", OFFICE,
     ("national security council", "nsc")),
    ("National Economic Council", OFFICE, ("national economic council", "nec")),
    ("Executive Office of the President", OFFICE,
     ("executive office of the president", "eop")),

    ("Environmental Protection Agency", OFFICE,
     ("environmental protection agency", "administrator of the environmental protection agency",
      "epa")),
    ("General Services Administration", OFFICE,
     ("general services administration", "administrator of general services", "gsa")),
    ("Small Business Administration", OFFICE,
     ("small business administration", "sba")),
    ("Federal Emergency Management Agency", OFFICE,
     ("federal emergency management agency", "fema")),
    ("Central Intelligence Agency", OFFICE, ("central intelligence agency", "cia")),
    ("Federal Bureau of Investigation", OFFICE,
     ("federal bureau of investigation", "fbi")),
    ("National Aeronautics and Space Administration", OFFICE,
     ("national aeronautics and space administration", "nasa")),
    ("National Science Foundation", OFFICE, ("national science foundation", "nsf")),
    ("Social Security Administration", OFFICE, ("social security administration", "ssa")),
    ("National Institutes of Health", OFFICE, ("national institutes of health", "nih")),
    ("Nuclear Regulatory Commission", OFFICE,
     ("nuclear regulatory commission", "nrc")),
    ("Director of the Federal Bureau of Investigation", OFFICIAL, ()),

    ("Assistant to the President for National Security Affairs", OFFICIAL,
     ("assistant to the president for national security affairs", "apnsa")),
    ("Assistant to the President for Science and Technology", OFFICIAL,
     ("assistant to the president for science and technology", "apst")),
    ("The President", OFFICIAL, ("the president", "president")),
    ("Vice President", OFFICIAL, ("vice president",)),

    ("All agencies (collective)", COLLECTIVE,
     ("all executive departments and agencies", "all federal departments and agencies",
      "all executive branch departments and agencies", "all executive departments",
      "all federal agencies", "all executive agencies", "all agencies",
      "executive departments and agencies", "federal agencies", "agency heads",
      "heads of executive departments and agencies", "heads of all agencies",
      "each agency", "all departments and agencies", "every agency",
      "heads of agencies", "all federal executive agencies")),
)

# Longest alias first: "health and human services" must win over "human services".
_ALIASES: list[tuple[str, str]] = sorted(
    ((alias, name) for name, _, aliases in _CANONICAL for alias in aliases),
    key=lambda pair: -len(pair[0]),
)
KINDS: dict[str, str] = {name: kind for name, kind, _ in _CANONICAL}

# Tokens that carry no restriction: a name built only from these means
# "the whole executive branch". Anything else surviving -- "contracting",
# "rulemaking", "permitting" -- names a *subset*, and folding those into the
# collective would claim every agency was tasked when only some were.
_GENERIC_TOKENS = frozenset({
    "all", "each", "every", "other", "the", "heads", "head", "of", "and",
    "departments", "department", "agencies", "agency", "entities", "entity",
    "offices", "office", "executive", "branch", "federal", "government",
    "united", "states", "us", "u", "s", "national", "relevant", "appropriate",
})
COLLECTIVE_NAME = "All agencies (collective)"

_PAREN_RE = re.compile(r"\([^)]*\)")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def clean(raw: str) -> str:
    """Reduce an extracted name to a comparable surface form.

    Parentheticals go first: they hold acronyms ("Office of Personnel Management
    (OPM)") and consultation clauses ("Secretary of the Treasury (in consultation
    with the Secretary of State)"), and neither changes which agency is named.
    """
    text = unicodedata.normalize("NFKD", raw or "")
    text = text.replace("’", "'").replace("‘", "'")
    text = _PAREN_RE.sub(" ", text).lower()
    text = _NON_WORD_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    for prefix in ("the ", "u s ", "us "):
        text = text.removeprefix(prefix)
    return text.strip()


def is_generic_collective(text: str) -> bool:
    """Whether a cleaned name means the executive branch as a whole.

    "each federal agency" and "all executive branch agencies" do; "all
    contracting agencies" and "heads of federal permitting agencies" do not --
    they name a subset, and the qualifier is the whole point.
    """
    tokens = text.split()
    if not tokens:
        return False
    if not any(t in {"agencies", "agency", "departments", "department"} for t in tokens):
        return False
    return all(token in _GENERIC_TOKENS for token in tokens)


def _word_bounded(alias: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None


def resolve(raw: str) -> tuple[list[str], bool]:
    """Canonical agencies named by one extracted string.

    Returns (names, matched). `matched` is False when nothing in the alias table
    was found, in which case the cleaned surface form is returned as a canonical
    entity of its own -- a one-off task force is not an error, and must stay
    countable and findable rather than being folded into a bucket.
    """
    text = clean(raw)
    if not text:
        return [], False

    found: list[str] = []
    remaining = text
    for alias, canonical in _ALIASES:
        if canonical in found:
            continue
        if _word_bounded(alias, remaining):
            found.append(canonical)
            # Blank the matched span so a shorter alias cannot re-match inside it.
            remaining = re.sub(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", " ", remaining
            )
    if found:
        return found, True
    if is_generic_collective(text):
        return [COLLECTIVE_NAME], True
    return [title_case(text)], False


def title_case(text: str) -> str:
    """Presentable form for a name that matched no alias."""
    small = {"of", "the", "and", "for", "on", "in", "to", "a", "an"}
    words = text.split()
    return " ".join(
        word.capitalize() if i == 0 or word not in small else word
        for i, word in enumerate(words)
    )


def kind_of(canonical: str) -> str:
    return KINDS.get(canonical, BODY)
