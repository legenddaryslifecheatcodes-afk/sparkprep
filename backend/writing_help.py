"""AI Writing Help for the Interior Studio: spelling/grammar checks and wording suggestions.

The editor sends the paragraphs in scope (a selection, a chapter, or one batch of a whole book) as
[{id, text}]. Claude answers with small, exact find-and-replace suggestions. Nothing here edits the book:
every suggestion is checked against the paragraph it names, so the editor can show it in place and the author
accepts or skips each one.
"""
from __future__ import annotations

import re

MODES = ("spelling", "wording")
KINDS = ("spelling", "grammar", "punctuation", "word_choice", "clarity", "repetition", "flow")

MAX_CHARS = 9000              # one request: about a chapter
MAX_PARAGRAPHS = 400
MAX_PARAGRAPH_CHARS = 4000
MAX_SUGGESTIONS = 60
DAILY_CHARS = 400_000         # per account per day: roughly a whole novel, both checks, with room to spare

_SPELLING_RULES = (
    "Find ONLY real mistakes: misspellings and typos, wrong words (their/there, its/it's, affect/effect), grammar "
    "errors (agreement, tense slips, missing or doubled words), and punctuation errors (missing full stops, wrong "
    "apostrophes, unbalanced quotation marks, comma splices). Do NOT change the author's style, voice or word "
    "choices. Leave dialect and slang in dialogue, deliberate sentence fragments, invented names and words, and "
    "consistent British or American spelling alone. If a paragraph has no mistakes, give nothing for it."
)
_WORDING_RULES = (
    "Suggest better wording only where it clearly helps the reader: a clunky or confusing sentence, a word repeated "
    "close together, a weak or vague word, a run-on that needs splitting, wordiness that can be trimmed. Keep the "
    "author's voice, tense, point of view and meaning exactly. Never rewrite a whole paragraph, never add new "
    "ideas, never change dialogue that sounds like how the character talks. Fewer, better suggestions beat many "
    "small ones: at most one or two per paragraph, and skip paragraphs that already read well. Also fix any plain "
    "spelling mistake you notice."
)


def clean_paragraphs(paragraphs) -> list[dict]:
    """Keep well-formed, non-empty paragraphs with unique ids, within the size limits."""
    out, seen, total = [], set(), 0
    for p in paragraphs or []:
        if not isinstance(p, dict):
            continue
        pid, text = p.get("id"), p.get("text")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid in seen or not isinstance(text, str):
            continue
        text = text[:MAX_PARAGRAPH_CHARS]
        if not text.strip():
            continue
        if total + len(text) > MAX_CHARS or len(out) >= MAX_PARAGRAPHS:
            break
        seen.add(pid)
        total += len(text)
        out.append({"id": pid, "text": text})
    return out


def chars(paragraphs: list[dict]) -> int:
    return sum(len(p["text"]) for p in paragraphs)


def build_prompt(mode: str, paragraphs: list[dict], genre: str | None = None, title: str | None = None) -> str:
    about = []
    if title:
        about.append(f"The book is called \"{title.strip()[:200]}\".")
    if genre:
        about.append(f"Its genre is {genre.strip()[:80]}.")
    numbered = "\n".join(f"[{p['id']}] {p['text']}" for p in paragraphs)
    job = "proofreader" if mode == "spelling" else "line editor"
    return (
        f"You are a careful {job} helping a self-published author get their book ready for print. "
        + " ".join(about) + "\n\n"
        + (_SPELLING_RULES if mode == "spelling" else _WORDING_RULES) + "\n\n"
        "Each paragraph below starts with its number in square brackets. For each change, give:\n"
        "- p: the paragraph number\n"
        "- find: the exact text to replace, copied character for character from that paragraph (same quotes, "
        "dashes and capitals). Keep it short: just the words that change plus a few around them so it appears "
        "only once in the paragraph. Never include the [number].\n"
        "- replace: what it should say instead\n"
        "- why: a short, friendly reason in plain English (under 15 words), e.g. \"Spelling\" or \"'Their' means "
        "belonging to them; here you want 'there'\"\n"
        f"- kind: one of {', '.join(KINDS)}\n\n"
        "Paragraphs:\n" + numbered + "\n\n"
        "Respond with ONLY a JSON object, no markdown fences, in exactly this shape:\n"
        '{"suggestions": [{"p": 0, "find": "...", "replace": "...", "why": "...", "kind": "spelling"}]}\n'
        'If there is nothing to change, respond {"suggestions": []}.'
    )


def _locate(text: str, find: str) -> int:
    """Where `find` sits in the paragraph, forgiving only curly-vs-straight quotes. -1 if absent."""
    i = text.find(find)
    if i >= 0:
        return i
    straight = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
    if len(find) == len(find.translate(straight)):
        return text.translate(straight).find(find.translate(straight))
    return -1


def clean_suggestions(raw, paragraphs: list[dict]) -> list[dict]:
    """Keep only suggestions that point at real text in a paragraph we sent, in reading order."""
    by_id = {p["id"]: p["text"] for p in paragraphs}
    order = {p["id"]: n for n, p in enumerate(paragraphs)}
    items = raw.get("suggestions") if isinstance(raw, dict) else None
    out, seen = [], set()
    for s in items if isinstance(items, list) else []:
        if not isinstance(s, dict):
            continue
        pid = s.get("p")
        if isinstance(pid, str) and pid.strip().isdigit():
            pid = int(pid.strip())
        find, replace = s.get("find"), s.get("replace")
        if pid not in by_id or not isinstance(find, str) or not isinstance(replace, str):
            continue
        find = re.sub(r"^\[\d+\]\s*", "", find)
        if not find.strip() or len(find) > 1500 or len(replace) > 2000 or find == replace:
            continue
        text = by_id[pid]
        at = _locate(text, find)
        if at < 0:
            continue
        find = text[at:at + len(find)]          # the paragraph's own characters, curly quotes and all
        if find == replace:
            continue
        key = (pid, at, find)
        if key in seen or any(o["p"] == pid and o["at"] < at + len(find) and at < o["at"] + len(o["find"]) for o in out):
            continue                            # duplicates and overlapping edits would fight each other
        seen.add(key)
        kind = s.get("kind") if s.get("kind") in KINDS else "word_choice"
        out.append({"p": pid, "at": at, "find": find, "replace": replace,
                    "why": str(s.get("why") or "").strip()[:200], "kind": kind})
    out.sort(key=lambda o: (order[o["p"]], o["at"]))
    return out[:MAX_SUGGESTIONS]
