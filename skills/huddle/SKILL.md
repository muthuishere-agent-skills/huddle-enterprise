---
name: huddle
description: >
  Autonomous, repo- and wiki-grounded answer engine. Trigger this skill when the user (or an
  upstream system) asks a question targeted at a corpus / party / department, like:
  "ask the wiki", "answer this from corpus X", "what does the team know about Y",
  "huddle, answer: ...", "respond to: ...", "give me a grounded answer about Z",
  "what's our position on Q", "summarise what we know about W", "research this from our docs".
  Distinct from the original discussion-style huddle: this variant does NOT stop and ask
  follow-up questions. It retrieves from the LLM wiki, runs a small society of personas
  internally, and emits one synthesized answer with dissent. It is the engine the Responser
  product and the headless `/loop` workflow call into.
---

# huddle (autonomous)

The huddle-enterprise variant of huddle. Not a discussion — an answer.

## What it is

Given a question and a corpus name, this skill produces one structured answer:

```
question + corpus  ──►  wiki retrieval (top-K chunks)
                  ──►  load persona roster
                  ──►  ONE structured Claude call:
                           pick 3 personas (Minsky-style agents),
                           each writes a perspective grounded in chunks,
                           synthesize one answer + dissent
                  ──►  record QA row in the wiki store
                  ──►  return JSON
```

## Society-of-Mind framing

- Each persona is an **agent** in Minsky's sense — narrow, opinionated, with its own scars.
- The question is a **K-line** — it activates the agents most relevant to itself.
- The synthesis is the **society's collective response** — one answer that respects tension between agents.
- Dissent is preserved, not flattened. If two agents disagree about how to answer, the dissent block records it.

The roster (21 personas) lives in the original huddle skill at `references/personas/`. The autonomous huddle reads frontmatter only — full persona bodies are loaded by Claude when generating a perspective.

## When to invoke this skill

Trigger this skill, NOT the original conversational huddle, when:
- The asker is **not the user driving the session** (it's an upstream API, a queue worker, the Responser product).
- The user explicitly wants a one-shot answer, not a discussion.
- A `/loop` or autonomous workflow needs an answer to push to an endpoint.

Trigger the **original** discussion huddle (in `huddle/` outside this repo) when the user wants to think out loud, debate, brainstorm, or drive the agenda.

## How to call it

**From inside this Claude Code session** (shell out):
```bash
{repo}/clis/huddle/bin/huddle ask <corpus> "<question>" --format json
```

The `--format human` view is what you show the user in chat. `--format json` is what an API consumer wants.

**From an external system (Responser API loop)**: same CLI, but capture stdout and POST to the delivery endpoint.

## What the answer contains

```json
{
  "answer":           "synthesized answer (paragraphs ok)",
  "personas_chosen":  ["maya", "shaama", "babu"],
  "reasoning":        "why these 3 fit this question",
  "perspectives":     [{"persona": "...", "view": "..."}, ...],
  "dissent":          [{"persona": "...", "concern": "..."}, ...],
  "chunk_refs":       ["chunk_id1", "chunk_id2"],
  "confidence":       "high | medium | low",
  "open_questions":   ["things the wiki didn't conclusively answer"]
}
```

## Wiki dependency

Requires a populated corpus in `~/.config/muthuishere-agent-skills/huddle-enterprise/{corpus}/`. If the corpus is empty or the wiki has no matching chunks, the answer will:
- still produce personas + perspectives (general knowledge)
- mark `confidence: low`
- list the gap in `open_questions`
- have empty `chunk_refs`

Use the **wiki-builder** skill to populate or update the corpus before asking, when relevant docs are available.

## What it does NOT do

- Does not ask the user follow-up questions (no stop-and-wait).
- Does not write to project files.
- Does not start a long-running discussion.
- Does not modify wiki chunks. Use **wiki-builder** for that.

## Past Q&A as state

The engine reads the 5 most recent Q&A entries for the corpus to inform follow-up questions ("what about X also?"). It writes every answer back as a `qa` row, so the next question has the prior context. This is how the skill is "stateful based on other data."

## Cost & latency

One `claude -p` call per question. With `--model sonnet`, expect ~10–30s and ~$0.01–$0.05 per answer. Tune with `--model opus` or `--max-budget-usd` if needed.

## Resume / replay

```bash
{repo}/clis/huddle/bin/huddle recent <corpus> --limit 20
```

Returns past Q&A as JSON for replay, audit, or fine-tuning.
