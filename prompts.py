"""Prompt templates. Keep outputs short, A2-friendly, structured.

The translator LLM gets ONE page worth of blocks and must return one Chinese
line per block, in JSON Lines format.
"""

from __future__ import annotations

import json


def page_translate_prompt(blocks: list[dict]) -> str:
    """blocks: [{kind: h2|p|caption, text or sents:[...]}]

    Ask LLM to return one Chinese string per block (same order, same count),
    as a JSON array on a single line. We parse that one line.
    """
    # Build a numbered list of blocks to translate.
    lines = []
    for idx, b in enumerate(blocks):
        kind = b["kind"]
        if kind in {"h2", "heading", "heading1", "heading2", "heading3"}:
            lines.append(f"[{idx}] HEADING: {b['text']}")
        elif kind == "caption":
            lines.append(f"[{idx}] CAPTION: {b['text']}")
        elif kind in {"p", "paragraph"}:
            text = " ".join(b.get("sents", [])) or b.get("text", "")
            lines.append(f"[{idx}] PARAGRAPH: {text}")
        else:
            text = b.get("text", "")
            lines.append(f"[{idx}] OTHER: {text}")

    numbered = "\n".join(lines)
    n = len(blocks)
    return (
        "You are a careful English-to-Chinese translator for a reading companion.\n"
        "Audience: a learner at A2 English level. Keep names/terms recognizable.\n"
        "Rules:\n"
        " - Translate ONLY. No commentary, no summary, no extra notes.\n"
        " - Preserve technical terms in English when there is no clear Chinese equivalent\n"
        "   (or give: Chinese (English)).\n"
        " - Headings stay short (under 12 Chinese characters where possible).\n"
        " - Paragraphs read naturally; do not invent missing information.\n"
        "Return a JSON array of exactly "
        f"{n} Chinese strings, in order, on a SINGLE LINE. No markdown fences.\n\n"
        f"INPUT:\n{numbered}\n\n"
        "OUTPUT (JSON array, single line):"
    )


def word_lookup_prompt(word: str, sentence: str) -> str:
    """Generate short zh + simple-en for a word in context."""
    return (
        "You build a useful vocabulary memory card for an English learner.\n"
        "The Chinese meaning is primary; do not only explain in English. Return JSON only with:\n"
        ' - "zh": 1-3 short Chinese meanings, most common first. This must be the actual meaning, never a part-of-speech label. For example, leisure -> "休闲；闲暇时间", never "名词 noun"\n'
        ' - "part_of_speech": concise Chinese/English part of speech, e.g. "名词 noun"\n'
        ' - "en_simple": a very short plain-English explanation (<= 12 words)\n'
        ' - "pronunciation": IPA pronunciation, e.g. /ˈliːʒər/\n'
        ' - "word_form": useful word-root/suffix or word-family clue; return content only, no "词形" label; if unclear, say "基础词"\n'
        ' - "memory_tip": one vivid Chinese association linking sound/form to meaning\n'
        "Keep every field short. Output one JSON object only, no markdown.\n\n"
        f'WORD: {word}\n'
        f"SENTENCE: {sentence}\n\n"
        "OUTPUT:"
    )


def sentence_help_prompt(sentence: str) -> str:
    """Return 4-tier help for one sentence."""
    return (
        "You help an A2-level English learner decode one sentence.\n"
        "Return a JSON object with exactly these keys:\n"
        ' - "keywords": array of 2-4 most important words/phrases in the sentence (in English)\n'
        ' - "structure": a short Chinese description of the sentence structure\n'
        '   (e.g. "主语: ... / 谓语: ... / 宾语: ...") under 30 Chinese chars\n'
        ' - "en_simple": a simple-English paraphrase at A2 level (<= 25 words)\n'
        ' - "zh": a natural Chinese translation\n'
        "Output JSON only, single line. No markdown.\n\n"
        f"SENTENCE: {sentence}\n\n"
        "OUTPUT:"
    )


def section_hint_prompt(section_title: str, first_paragraph: str) -> str:
    """One short Chinese sentence describing what this section is about."""
    return (
        "You write a one-sentence Chinese hint describing what an academic paper section\n"
        "is about, for an A2-level reader. Keep it under 30 Chinese characters.\n"
        "Output only the hint, no quotes, no markdown.\n\n"
        f"SECTION TITLE: {section_title}\n"
        f"FIRST PARAGRAPH (first 200 chars): {first_paragraph[:200]}\n\n"
        "OUTPUT:"
    )


# ---- Parsing helpers ------------------------------------------------------


def parse_json_line(text: str) -> dict | list | None:
    """Parse a single JSON line from LLM output. Tolerant of stray whitespace."""
    text = text.strip()
    # Strip markdown fences if the model added them
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find first { or [ and last } or ]
        for opener, closer in [("{", "}"), ("[", "]")]:
            i = text.find(opener)
            j = text.rfind(closer)
            if i != -1 and j != -1 and j > i:
                try:
                    return json.loads(text[i : j + 1])
                except json.JSONDecodeError:
                    continue
        return None


def parse_json_array(text: str, expected_count: int) -> list[str]:
    """Parse a JSON array of strings; fall back to splitting lines if needed."""
    parsed = parse_json_line(text)
    if isinstance(parsed, list):
        out = [str(x) for x in parsed]
        if len(out) == expected_count:
            return out
        # Pad or trim
        while len(out) < expected_count:
            out.append("")
        return out[:expected_count]
    # Fallback: split by newlines
    parts = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(parts) == expected_count:
        return parts
    return [""] * expected_count
