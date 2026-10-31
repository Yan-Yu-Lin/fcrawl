#!/usr/bin/env python3
"""fcrawl gate — one script for three hook events.

PostToolUse[Skill]           fcrawl skill loaded successfully → touch a marker
                             (covers the transcript's async write lag).
PreToolUse[WebSearch|WebFetch]
PreToolUse[Bash] (fcrawl …)  deny unless the fcrawl skill text is in THIS agent's
                             context window *since its last compaction*; after
                             that stay silent (normal permissions).
SessionStart[compact]        re-inject the hard rules as additionalContext so the
                             agent is never left with no rules at all.

Why the "since last compaction" test (measured, not from the docs): after a
compaction the skill text is only re-attached for a main session that was never
resumed/forked; resumed sessions and subagents lose it entirely. Re-invoking the
Skill always re-sends the full text, so we just make the agent do that once per
compaction. Subagents report the main session_id, so every key includes agent_id
and we read the subagent's own transcript under <session>/subagents/.

Runs on the system python3 (macOS ships 3.9): stdlib only, and the annotations
are strings (`from __future__`) so `Path | None` does not blow up at import.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import time

SKILL = "fcrawl"
# the same directory `fcrawl setup claude-code` installed into (CLAUDE_CONFIG_DIR is
# what Claude Code itself honours; the hook inherits its environment)
CLAUDE_DIR = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or (pathlib.Path.home() / ".claude"))
SKILL_FILE = CLAUDE_DIR / "skills" / SKILL / "SKILL.md"
STATE_DIR = CLAUDE_DIR / "state" / "fcrawl-gate"
GATED_TOOLS = {"WebSearch", "WebFetch"}
TRACE = (STATE_DIR / "TRACE").exists()
FCRAWL_CMD = re.compile(r"(^|[\s;&|(`])fcrawl(\s|$)")

DENY_REASON = (
    "The fcrawl rules are not in your current context (either this agent never "
    "loaded the skill, or they were dropped at compaction). Web access on this "
    "machine goes through the `fcrawl` CLI with specific rules (search results "
    "stay in Bash output, scrape with --save-temp, never --no-links on docs). "
    "Call the Skill tool with skill=\"fcrawl\" — it re-sends the full text — then "
    "retry this call. Each subagent loads it separately."
)


# --- state ----------------------------------------------------------------


def agent_transcript(d: dict) -> pathlib.Path | None:
    tp = d.get("transcript_path")
    if not tp:
        return None
    tp = pathlib.Path(tp)
    aid = d.get("agent_id")
    if aid:
        return tp.parent / tp.stem / "subagents" / f"agent-{aid}.jsonl"
    return tp


def marker_for(d: dict) -> pathlib.Path:
    sid = d.get("session_id") or "nosession"
    aid = d.get("agent_id") or "main"
    return STATE_DIR / f"{sid}__{aid}"


def scan_cache_path(tp: pathlib.Path) -> pathlib.Path:
    return STATE_DIR / f"scan-{hashlib.sha1(str(tp).encode()).hexdigest()[:12]}.json"


def is_fcrawl_load(line: bytes) -> bool:
    try:
        rec = json.loads(line)
    except ValueError:
        return False
    for block in (rec.get("message") or {}).get("content") or []:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "Skill"
            and (block.get("input") or {}).get("skill") == SKILL
        ):
            return True
    att = rec.get("attachment") or {}
    if att.get("type") == "invoked_skills":
        for s in att.get("skills") or []:
            if s.get("name") == SKILL and "truncated for compaction" not in (s.get("content") or ""):
                return True
    return False


def loaded_in_window(d: dict) -> bool:
    """True when this agent has the full fcrawl text in context since its last compaction.

    Incremental scan of the agent's own transcript; a compact_boundary resets the
    flag. The PostToolUse marker covers the write lag of the transcript.
    """
    tp = agent_transcript(d)
    mark = marker_for(d)
    if tp is None or not tp.exists():
        return mark.exists()

    cache = scan_cache_path(tp)
    st = {"off": 0, "ok": False, "bt": 0.0}
    try:
        st.update(json.loads(cache.read_text()))
    except (OSError, ValueError):
        pass

    size = tp.stat().st_size
    if st["off"] > size:  # file rewritten
        st = {"off": 0, "ok": False, "bt": 0.0}
    with tp.open("rb") as fh:
        fh.seek(st["off"])
        for line in fh:
            if b'"compact_boundary"' in line:
                st["ok"] = False
                st["bt"] = time.time()
            elif b"fcrawl" in line and is_fcrawl_load(line):
                st["ok"] = True
        st["off"] = fh.tell()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(st))

    if st["ok"]:
        return True
    return mark.exists() and mark.stat().st_mtime > st["bt"]


# --- events ---------------------------------------------------------------


def hard_rules() -> str:
    """The 'Hard rules' section of SKILL.md, for re-injection after compaction."""
    try:
        text = SKILL_FILE.read_text()
    except OSError:
        return ""
    m = re.search(r"## Hard rules.*?(?=\n## )", text, re.S)
    return m.group(0).strip() if m else ""


def main() -> None:
    try:
        d = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    event = d.get("hook_event_name")
    tool = d.get("tool_name")
    if TRACE:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with (STATE_DIR / "trace.log").open("a") as fh:
                fh.write(json.dumps({"t": time.time(), "ev": event, "tool": tool,
                                     "aid": d.get("agent_id"), "cmd": (d.get("tool_input") or {}).get("command", "")[:60]}) + "\n")
        except OSError:
            pass

    if event == "PostToolUse" and tool == "Skill":
        if (d.get("tool_input") or {}).get("skill") == SKILL and (
            d.get("tool_response") or {}
        ).get("success", True):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            marker_for(d).touch()
        sys.exit(0)

    if event == "SessionStart" and d.get("source") == "compact":
        rules = hard_rules()
        if rules:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        "Context was just compacted. The fcrawl skill text may have been dropped; "
                        "these are its hard rules. For the full command reference call the Skill "
                        "tool with skill=\"fcrawl\" again (it re-sends the full text).\n\n" + rules
                    ),
                }
            }))
        sys.exit(0)

    if event == "PreToolUse":
        gated = tool in GATED_TOOLS
        if tool == "Bash":
            gated = bool(FCRAWL_CMD.search((d.get("tool_input") or {}).get("command") or ""))
        if not gated:
            sys.exit(0)
        try:
            ok = loaded_in_window(d)
        except Exception:
            ok = True  # fail open: a broken gate must not block the web
        if TRACE:
            with (STATE_DIR / "trace.log").open("a") as fh:
                fh.write(json.dumps({"decision": "allow" if ok else "deny", "aid": d.get("agent_id")}) + "\n")
        if ok:
            sys.exit(0)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": DENY_REASON,
            }
        }))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
