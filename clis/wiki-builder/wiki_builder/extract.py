"""Headless Claude calls to extract structured knowledge from a chunk of text."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary":     {"type": "string", "description": "One-paragraph synthesis of the chunk"},
        "section":     {"type": "string", "description": "Section or chapter heading if detectable, else empty"},
        "concepts":    {"type": "array", "items": {"type": "string"},
                        "description": "Short canonical names of concepts introduced or used"},
        "definitions": {"type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Map of concept name → definition extracted from this chunk"},
        "claims":      {"type": "array", "items": {"type": "string"},
                        "description": "Standalone factual claims or arguments the chunk makes"},
        "examples":    {"type": "array", "items": {"type": "string"},
                        "description": "Concrete examples or illustrations"},
        "xrefs":       {"type": "array", "items": {"type": "string"},
                        "description": "Concepts referenced but defined elsewhere"},
    },
    "required": ["summary", "concepts", "definitions", "claims", "examples", "xrefs"],
    "additionalProperties": False,
}

EXTRACTION_PROMPT = """You are extracting structured knowledge from a chunk of a document so it can be stored in an LLM-queryable wiki.

Return ONLY JSON matching the provided schema. No prose around it.

Rules:
- `concepts`: short canonical names (2-5 words), the ideas introduced or centrally used in this chunk.
- `definitions`: only definitions actually supported by the chunk text — do not invent.
- `claims`: standalone factual or argumentative statements (1-2 sentences each).
- `examples`: short concrete illustrations from the chunk.
- `xrefs`: concept names mentioned but not defined here (likely defined elsewhere in the document).
- `section`: best-guess section/chapter heading from the chunk text; empty string if unclear.
- `summary`: one paragraph (≤120 words) faithful to the chunk.

Chunk text follows the marker `===CHUNK===`.

===CHUNK===
{chunk_text}
"""


@dataclass
class Extraction:
    summary: str
    section: str
    concepts: list[str]
    definitions: dict[str, str]
    claims: list[str]
    examples: list[str]
    xrefs: list[str]

    @classmethod
    def empty(cls) -> "Extraction":
        return cls(summary="", section="", concepts=[], definitions={},
                   claims=[], examples=[], xrefs=[])


class ExtractError(RuntimeError):
    pass


def claude_bin() -> str:
    bin_path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not bin_path:
        raise ExtractError(
            "claude CLI not found. Set CLAUDE_BIN or install Claude Code."
        )
    return bin_path


def extract_chunk(
    chunk_text: str,
    *,
    model: str = "sonnet",
    max_budget_usd: float = 0.10,
    timeout_s: int = 180,
) -> Extraction:
    prompt = EXTRACTION_PROMPT.format(chunk_text=chunk_text)
    cmd = [
        claude_bin(),
        "-p",
        "--bare",
        "--output-format", "json",
        "--json-schema", json.dumps(EXTRACTION_SCHEMA),
        "--model", model,
        "--max-budget-usd", str(max_budget_usd),
        "--no-session-persistence",
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ExtractError(f"claude timed out after {timeout_s}s") from e
    if proc.returncode != 0:
        raise ExtractError(
            f"claude exit {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return _parse(proc.stdout)


def _parse(stdout: str) -> Extraction:
    raw = stdout.strip()
    if not raw:
        raise ExtractError("empty response from claude")
    # claude -p --output-format json wraps the result. The schema-validated payload
    # lives in `result` (string of JSON) or in `result_json` depending on version.
    payload: Any
    try:
        env = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ExtractError(f"non-JSON response: {raw[:200]}") from e
    if isinstance(env, dict) and "result" in env:
        result = env["result"]
        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                # Some models return prose around JSON despite schema; try recovery.
                payload = _recover_json(result)
        else:
            payload = result
    else:
        payload = env

    if not isinstance(payload, dict):
        raise ExtractError(f"extraction payload not a dict: {type(payload).__name__}")

    return Extraction(
        summary=str(payload.get("summary", "")),
        section=str(payload.get("section", "")),
        concepts=[str(x) for x in payload.get("concepts", [])],
        definitions={str(k): str(v) for k, v in (payload.get("definitions") or {}).items()},
        claims=[str(x) for x in payload.get("claims", [])],
        examples=[str(x) for x in payload.get("examples", [])],
        xrefs=[str(x) for x in payload.get("xrefs", [])],
    )


def _recover_json(s: str) -> dict:
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        raise ExtractError(f"no JSON object found in: {s[:200]}")
    return json.loads(s[start:end + 1])
