"""Autonomous huddle answer engine.

Pipeline:
  question  ──►  query wiki (top-K chunks)
            ──►  load persona roster (frontmatter only)
            ──►  load recent Q&A (state)
            ──►  ONE claude -p call with structured schema
            ──►  record QA row, return result
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Cross-package import: wiki-builder lives next door under clis/wiki-builder.
# We add it to sys.path when needed.
def _ensure_wiki_builder_path() -> None:
    import sys
    here = Path(__file__).resolve()
    wb = here.parents[3] / "wiki-builder"
    if wb.exists() and str(wb) not in sys.path:
        sys.path.insert(0, str(wb))


_ensure_wiki_builder_path()
from wiki_builder import storage as wiki_storage  # noqa: E402

from .personas import load_all, PersonaSummary


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "personas_chosen": {
            "type": "array", "items": {"type": "string"},
            "description": "Persona keys (filename stems) chosen for this question. Pick 3.",
        },
        "reasoning": {"type": "string", "description": "Why these 3 personas fit this question"},
        "perspectives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string"},
                    "view": {"type": "string"},
                },
                "required": ["persona", "view"],
                "additionalProperties": False,
            },
            "description": "Each chosen persona's perspective on the question, in their voice, grounded in the chunks.",
        },
        "answer": {"type": "string", "description": "Synthesized best answer for the asker"},
        "dissent": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "persona": {"type": "string"},
                    "concern": {"type": "string"},
                },
                "required": ["persona", "concern"],
                "additionalProperties": False,
            },
            "description": "Where personas qualify, push back on, or disagree with the synthesized answer. May be empty if there is no real tension.",
        },
        "chunk_refs": {
            "type": "array", "items": {"type": "string"},
            "description": "chunk_id values from the wiki that grounded the answer",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "open_questions": {
            "type": "array", "items": {"type": "string"},
            "description": "Things the wiki didn't conclusively answer",
        },
        "next_branches": {
            "type": "array",
            "minItems": 2, "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "question":       {"type": "string", "description": "The likely next question from the asker"},
                    "hook_in_answer": {"type": "string", "description": "The exact sentence in `answer` that subtly invites this branch"},
                    "why_likely":     {"type": "string", "description": "Why this is the most-likely next question after reading the answer"},
                },
                "required": ["question", "hook_in_answer", "why_likely"],
                "additionalProperties": False,
            },
            "description": "Binary tree of likely next questions. The answer must weave a subtle hook sentence for EACH — the asker should feel they chose the next question; in fact the answer led them there.",
        },
    },
    "required": [
        "personas_chosen", "reasoning", "perspectives", "answer",
        "dissent", "chunk_refs", "confidence", "open_questions",
        "next_branches",
    ],
    "additionalProperties": False,
}


SYSTEM_FRAMING = """You answer questions for an autonomous huddle (Minsky's Society of Mind framing). You must return ONLY a single JSON object — no markdown, no prose, no code fences. Start your response with '{' and end with '}'."""


JSON_RULES = """
OUTPUT FORMAT — STRICT
You MUST return EXACTLY one JSON object matching this schema. No prose before or after.
No ```json fences. No headings. No commentary. Begin with `{` and end with `}`.

{
  "personas_chosen":  [str, str, str],
  "reasoning":        str,
  "perspectives":     [{"persona": str, "view": str}, ...],
  "answer":           str,
  "dissent":          [{"persona": str, "concern": str}, ...],
  "chunk_refs":       [str, ...],
  "confidence":       "high" | "medium" | "low",
  "open_questions":   [str, ...],
  "next_branches":    [                              // exactly TWO
    {"question": str, "hook_in_answer": str, "why_likely": str},
    {"question": str, "hook_in_answer": str, "why_likely": str}
  ]
}
"""

TASK_INSTRUCTIONS = """
TASK
1. Read the question, wiki chunks, recent Q&A, and persona roster below.
2. Pick exactly 3 personas (by `key`) whose lenses are most relevant and diverse — they should plausibly disagree.
3. For each chosen persona, write a perspective in that persona's voice and principles, grounded in the chunks. Reference chunk_ids in chunk_refs.
4. Synthesize ONE direct, useful answer to the question — see ANSWER VOICE below.
5. Capture remaining dissent — where any chosen persona still pushes back on the synthesized answer. Do not manufacture dissent; if all three converge, leave dissent empty.
6. State confidence ("high"/"medium"/"low") and any open_questions the wiki did not conclusively answer.

Do NOT invent facts not present in the chunks. If the wiki is silent on the question, set confidence="low" and explain in open_questions.

ANSWER VOICE — applies ONLY to the `answer` field, not perspectives or dissent
The `answer` is read by senior, sophisticated people — founders, investors, policy heads, technical leaders. Write it as if it will be read aloud at a podium, not skimmed in a wiki. Models for cadence (absorb the rhythm, don't quote them):
- Barack Obama: measured, pivots from one concrete fact to a wider principle, parallel structures, accessible but elevated.
- K. Annamalai: direct, fact-loaded, names companies and numbers and dates without softening.
- Shashi Tharoor: erudite economy of language; one strong word over three weak ones; balanced sentences.

Rules for the `answer` field:
- Paragraphs only. No bullet lists. No markdown headers. No `**bold**`. No emojis.
- Three short paragraphs, four maximum. Frame, then ground in concrete evidence, then commit to a position.
- Every claim names something specific: a company, a number, a date, a chunk concept. No "many studies", "experts agree", "research shows".
- Use rhythm: a short sentence after a long one. Repetition with variation. One rhetorical pivot per paragraph.
- Banned phrasings: "In conclusion", "Three key reasons", "It is worth noting", "On the other hand", "Furthermore", "Moreover", "Additionally", "In summary".
- End on a sentence that could be quoted — a verdict, an image, or a question that lands.

Perspectives and dissent stay in each persona's own voice — do not apply the speech voice to them.

BINARY-TREE NEXT-BRANCH DESIGN
The asker thinks they drive the conversation; in fact, your answer drives it. After writing the answer, identify the TWO most-likely next questions a thoughtful reader would ask. Then ensure each branch has ONE corresponding sentence inside the `answer` that subtly invites it — an open thread, a strategic omission, an evocative phrase, a verdict that begs a "why" or a "but what about". These sentences must read as natural prose, not as labels. Do NOT write "you might ask next" or "this raises the question". The hook is a sentence already in `answer`; you simply name which sentence carries it.

Return both branches under `next_branches`. Each has the question, the exact hook sentence as it appears in `answer`, and one line on why this branch is likely.
"""


@dataclass
class Answer:
    question: str
    answer: str
    personas_chosen: list[str]
    perspectives: list[dict]
    dissent: list[dict]
    chunk_refs: list[str]
    confidence: str
    open_questions: list[str]
    reasoning: str
    raw_chunks: list[dict] = field(default_factory=list)
    amara_research: dict | None = None      # auto-populated when room-level research is triggered
    next_branches: list[dict] = field(default_factory=list)  # binary tree of likely next Qs
    cached: bool = False                    # True when returned from QA cache

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "personas_chosen": self.personas_chosen,
            "reasoning": self.reasoning,
            "perspectives": self.perspectives,
            "dissent": self.dissent,
            "chunk_refs": self.chunk_refs,
            "confidence": self.confidence,
            "open_questions": self.open_questions,
            "amara_research": self.amara_research,
            "next_branches": self.next_branches,
            "cached": self.cached,
        }

    @classmethod
    def from_cached_payload(cls, payload: dict, question: str) -> "Answer":
        return cls(
            question=payload.get("question") or question,
            answer=str(payload.get("answer") or ""),
            personas_chosen=list(payload.get("personas_chosen") or []),
            perspectives=list(payload.get("perspectives") or []),
            dissent=list(payload.get("dissent") or []),
            chunk_refs=list(payload.get("chunk_refs") or []),
            confidence=str(payload.get("confidence") or "medium"),
            open_questions=list(payload.get("open_questions") or []),
            reasoning=str(payload.get("reasoning") or ""),
            amara_research=payload.get("amara_research"),
            next_branches=list(payload.get("next_branches") or []),
            cached=True,
        )


class HuddleError(RuntimeError):
    pass


def claude_bin() -> str:
    bp = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not bp:
        raise HuddleError("claude CLI not found; set CLAUDE_BIN or install Claude Code")
    return bp


RESEARCH_TRIGGERS = [
    # existence / naming
    "does anyone", "do any ", "is there a", "is there an", "are there ",
    "name a ", "name the ", "named ", "named,", "named specific", "named, specific",
    # buyer / payment / demand
    "who is paying", "who would pay", "willing to pay",
    "real demand", "real customer", "real buyer",
    "actual customer", "actual buyer", "actual human",
    "use cases today", "customers today", "buyers today",
    # market existence
    "does this exist", "does this market exist", "market exist",
    "market for ", "exists ", "exists?", "exist?",
    # current state / adoption
    "currently", "today,", " today.", " today ", "as of ", "right now",
    "adoption", "widespread", "widely used",
    # pricing / revenue
    "pricing", "price ", " arr ", "funding", "revenue",
    # comparable products
    "products like", "any product", "comparable", "competitor",
    "who is shipping", "who's shipping", "shipping this",
]


def _needs_research(*, dissent: list[dict], open_questions: list[str],
                    perspectives: list[dict]) -> list[str]:
    """Return the list of room questions worth researching (lowercased substrings of pulled text)."""
    candidates: list[str] = []
    for d in dissent or []:
        s = d.get("concern") or ""
        if s:
            candidates.append(s)
    for q in open_questions or []:
        if q:
            candidates.append(q)
    for p in perspectives or []:
        v = p.get("view") or ""
        # only treat as researchable if it asks something
        if "?" in v and any(t in v.lower() for t in RESEARCH_TRIGGERS):
            candidates.append(v)
    worthy: list[str] = []
    for c in candidates:
        cl = c.lower()
        if any(t in cl for t in RESEARCH_TRIGGERS):
            worthy.append(c)
    # dedupe while preserving order
    seen = set()
    out: list[str] = []
    for s in worthy:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


AMARA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "open_question": {"type": "string"},
                    "evidence":      {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url":   {"type": "string"},
                            },
                            "required": ["title", "url"],
                            "additionalProperties": False,
                        },
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["supports_answer", "contradicts_answer", "inconclusive"],
                    },
                },
                "required": ["open_question", "evidence", "sources", "verdict"],
                "additionalProperties": False,
            },
        },
        "named_buyers": {"type": "array", "items": {"type": "string"}},
        "summary":      {"type": "string"},
    },
    "required": ["findings", "named_buyers", "summary"],
    "additionalProperties": False,
}


AMARA_INSTRUCTIONS = """You are Amara, the trend researcher in an autonomous huddle. The room has just synthesized an answer and left some open or contested questions on the table.

Your job: use the WebSearch tool to fact-check the room's open questions with live evidence. Bring named companies, named products, public pricing, customer counts, funding/ARR — whatever fits.

Speed rules (STRICT — the room is waiting on you):
- Make AT MOST 2 WebSearch calls total across the whole task. Pick the 2 questions that most need external evidence.
- For other open questions, return a finding with verdict "inconclusive" and a brief note that they need a separate research pass.
- After the 2 searches, IMMEDIATELY return JSON. Do not keep searching for completeness.

Other rules:
- Do not invent sources. Every URL must come from a WebSearch result.
- Populate `named_buyers` only when a question was asking who pays / who would buy / who the customer is. Otherwise leave it empty.
- Keep `summary` to two sentences max.

OUTPUT FORMAT — STRICT
Return ONLY one JSON object. No markdown. No prose. Begin with `{`, end with `}`. Shape:

{
  "findings": [
    {
      "open_question": "<question text>",
      "evidence":      "<1-3 sentence summary of what searches found>",
      "sources":       [{"title": "<source title>", "url": "<source url>"}, ...],
      "verdict":       "supports_answer" | "contradicts_answer" | "inconclusive"
    }
  ],
  "named_buyers": ["<company name>", ...],
  "summary":      "<one short paragraph synthesis>"
}
"""


def _amara_research(
    *,
    question: str,
    answer: str,
    open_qs: list[str],
    model: str = "sonnet",  # caller's model — but Amara always uses haiku for speed
    timeout_s: int = 150,
    max_budget_usd: float = 0.50,
) -> dict | None:
    prompt_lines = [
        AMARA_INSTRUCTIONS,
        "",
        "===ORIGINAL QUESTION===",
        question,
        "",
        "===ROOM'S SYNTHESIZED ANSWER (for context)===",
        answer[:1500] + ("…" if len(answer) > 1500 else ""),
        "",
        "===OPEN QUESTIONS TO RESEARCH===",
    ]
    for i, q in enumerate(open_qs, 1):
        prompt_lines.append(f"{i}. {q}")
    prompt_lines.append("")
    prompt_lines.append("Run WebSearch and return ONLY the JSON object specified by the schema.")
    prompt = "\n".join(prompt_lines)

    cmd = [
        claude_bin(),
        "-p",
        "--bare",
        "--allowedTools", "WebSearch",
        "--output-format", "json",
        "--model", "haiku",  # speed beats reasoning depth here; the room already reasoned
        "--max-budget-usd", str(max_budget_usd),
        "--no-session-persistence",
        prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"findings": [], "named_buyers": [],
                "summary": f"Amara timed out after {timeout_s}s — no evidence gathered.",
                "error": "timeout"}
    if proc.returncode != 0:
        return {"findings": [], "named_buyers": [],
                "summary": f"Amara research failed (exit {proc.returncode}).",
                "error": (proc.stderr.strip() or proc.stdout.strip())[:600]}
    try:
        payload = _parse(proc.stdout)
    except HuddleError as e:
        return {"findings": [], "named_buyers": [],
                "summary": f"Amara returned unparseable output: {e}",
                "error": str(e)}
    # Normalize
    return {
        "findings":     [dict(f) for f in (payload.get("findings") or [])],
        "named_buyers": [str(b) for b in (payload.get("named_buyers") or [])],
        "summary":      str(payload.get("summary") or ""),
    }


def ask(
    corpus: str,
    question: str,
    *,
    k: int = 5,
    model: str = "sonnet",
    max_budget_usd: float = 0.20,
    timeout_s: int = 240,
    record: bool = True,
    research: bool = True,
    use_cache: bool = True,
    cache_max_age_hours: int = 168,
    speculative: bool = True,
    _speculative_depth: int = 0,
) -> Answer:
    # 0. QA cache hit — instant return if we've answered this recently.
    if use_cache and _speculative_depth == 0:
        with wiki_storage.Store(corpus, read_only=True) as s:
            cached = s.find_cached_qa(question, max_age_hours=cache_max_age_hours)
        if cached and cached.get("payload"):
            return Answer.from_cached_payload(cached["payload"], question=question)
    # 1. Retrieve from wiki
    with wiki_storage.Store(corpus, read_only=True) as s:
        chunks = s.search(question, k=k)
        recent = s.recent_qa(limit=5)

    # 2. Load persona roster
    personas = load_all()

    # 3. Build prompt
    prompt = _build_prompt(
        question=question,
        chunks=chunks,
        recent=recent,
        personas=personas,
    )

    # 4. Call claude with schema
    cmd = [
        claude_bin(),
        "-p",
        "--bare",
        "--output-format", "json",
        "--json-schema", json.dumps(ANSWER_SCHEMA),
        "--model", model,
        "--max-budget-usd", str(max_budget_usd),
        "--append-system-prompt", SYSTEM_FRAMING,
        "--no-session-persistence",
        prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        raise HuddleError(f"claude timed out after {timeout_s}s") from e
    if proc.returncode != 0:
        raise HuddleError(
            f"claude exit {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )

    payload = _parse(proc.stdout)
    ans = Answer(
        question=question,
        answer=str(payload["answer"]),
        personas_chosen=list(payload["personas_chosen"]),
        perspectives=list(payload["perspectives"]),
        dissent=list(payload["dissent"]),
        chunk_refs=list(payload["chunk_refs"]),
        confidence=str(payload["confidence"]),
        open_questions=list(payload["open_questions"]),
        reasoning=str(payload["reasoning"]),
        raw_chunks=chunks,
        next_branches=list(payload.get("next_branches") or []),
    )

    # 5. Room-level auto-research: if any open question / dissent looks
    #    web-researchable, hand off to Amara.
    if research:
        worthy = _needs_research(
            dissent=ans.dissent,
            open_questions=ans.open_questions,
            perspectives=ans.perspectives,
        )
        if worthy:
            ans.amara_research = _amara_research(
                question=ans.question,
                answer=ans.answer,
                open_qs=worthy,
                model=model,
            )

    # 6. Record to QA store (full payload so the cache returns everything)
    if record:
        with wiki_storage.Store(corpus) as s:
            s.record_qa(
                question=question,
                answer=ans.answer,
                dissent=ans.dissent,
                personas=ans.personas_chosen,
                chunk_refs=ans.chunk_refs,
                payload=ans.to_dict(),
            )

    # 7. Speculative pre-warm: detached background processes that fill the QA cache
    #    for each next-branch. One level deep — never recursive.
    if speculative and _speculative_depth == 0 and ans.next_branches:
        _spawn_speculative_prewarm(corpus, ans.next_branches, model=model)

    return ans


def _spawn_speculative_prewarm(corpus: str, branches: list[dict],
                                *, model: str = "sonnet") -> None:
    """Fire-and-forget background jobs to pre-compute next-branch answers.

    Logs go to /tmp/huddle-speculative-{ts}.log so failures aren't silent.
    """
    import os as _os
    import subprocess as sp
    import time as _t
    me = Path(__file__).resolve()
    bin_huddle = me.parents[2] / "bin" / "huddle"
    if not bin_huddle.exists():
        return
    log_dir = Path(_os.environ.get("TMPDIR", "/tmp"))
    ts = int(_t.time())
    for i, b in enumerate(branches[:2]):
        q = (b.get("question") or "").strip()
        if not q:
            continue
        log_path = log_dir / f"huddle-speculative-{ts}-{i}.log"
        try:
            log = open(log_path, "w")
            sp.Popen(
                [
                    str(bin_huddle), "ask", corpus, q,
                    "--no-research",
                    "--format", "json",
                    "--model", model,
                    "--no-speculative",
                ],
                stdout=log, stderr=sp.STDOUT,
                stdin=sp.DEVNULL, start_new_session=True,
            )
        except Exception:
            pass


def _build_prompt(
    *,
    question: str,
    chunks: list[dict],
    recent: list[dict],
    personas: list[PersonaSummary],
) -> str:
    lines: list[str] = []
    lines.append(JSON_RULES)
    lines.append(TASK_INSTRUCTIONS)
    lines.append("")
    lines.append("===QUESTION===")
    lines.append(question)
    lines.append("")
    lines.append("===WIKI CHUNKS (top-K, ranked)===")
    if not chunks:
        lines.append("(no chunks matched; the wiki may not cover this topic)")
    for c in chunks:
        lines.append(f"--- chunk_id: {c['chunk_id']}  pp.{c.get('page_start')}-{c.get('page_end')}  section: {c.get('section') or ''}")
        if c.get("summary"):
            lines.append(f"SUMMARY: {c['summary']}")
        if c.get("concepts"):
            lines.append(f"CONCEPTS: {', '.join(c['concepts'])}")
        if c.get("definitions"):
            for k, v in (c["definitions"] or {}).items():
                lines.append(f"DEF[{k}]: {v}")
        if c.get("claims"):
            for cl in c["claims"]:
                lines.append(f"CLAIM: {cl}")
        # Trim raw text to keep prompt size sane.
        text = c.get("text") or ""
        if len(text) > 1200:
            text = text[:1200] + "…"
        lines.append("TEXT:")
        lines.append(text)
        lines.append("")
    lines.append("===RECENT Q&A (this corpus, may inform follow-ups)===")
    if not recent:
        lines.append("(none)")
    for qa in recent:
        lines.append(f"Q: {qa['question']}")
        a = qa.get("answer") or ""
        if len(a) > 300:
            a = a[:300] + "…"
        lines.append(f"A: {a}")
        lines.append("")
    lines.append("===PERSONA ROSTER===")
    for p in personas:
        lines.append(p.to_brief())
    lines.append("")
    lines.append("REMINDER: Return ONLY the JSON object. No prose, no markdown, no code fences.")
    return "\n".join(lines)


def _parse(stdout: str) -> dict:
    raw = stdout.strip()
    if not raw:
        raise HuddleError("empty response from claude")
    try:
        env = json.loads(raw)
    except json.JSONDecodeError as e:
        # Some responses aren't wrapped — try direct recovery
        return _recover_json(raw)
    if isinstance(env, dict) and "result" in env:
        result = env["result"]
        if isinstance(result, str):
            return _recover_json(result)
        return result if isinstance(result, dict) else {"answer": str(result)}
    return env


def _recover_json(s: str) -> dict:
    """Recover JSON from a possibly-prosy / mildly-broken response.

    Handles: ```json fences, leading/trailing prose, smart quotes,
    and (via json-repair) unescaped inner quotes in string values.
    """
    s = s.strip()
    # Normalize smart quotes which sometimes sneak into model output.
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    # Strip ```json ... ``` fences if present
    if "```" in s:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, _re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        raise HuddleError(f"no JSON object in response: {s[:200]}")
    candidate = s[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Last chance: json-repair tolerates unescaped quotes, missing commas,
    # trailing commas, and a handful of other model mishaps.
    try:
        import json_repair
        repaired = json_repair.loads(candidate)
        if isinstance(repaired, dict):
            return repaired
    except Exception as e:
        raise HuddleError(f"json-repair could not salvage response: {e}; head: {candidate[:200]}") from e
    raise HuddleError(f"recovered string not valid JSON; head: {candidate[:200]}")
