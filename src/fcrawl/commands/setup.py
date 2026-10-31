"""`fcrawl setup claude-code`: install the Claude Code integration for a new user.

What a working install looks like (Arthur's machine is the reference):

  ~/.claude/skills/fcrawl/SKILL.md     the skill (shipped in this package as the
                                       single source of truth)
  ~/.claude/hooks/fcrawl_gate.py       the gate: denies WebSearch/WebFetch and
                                       `fcrawl …` in Bash until the skill was
                                       loaded in that agent; re-injects the hard
                                       rules after compaction
  ~/.claude/settings.json              four hook entries pointing at the gate
  ~/.claude/CLAUDE.md                  a short "## fcrawl" section

Everything is idempotent: run it again after `uv tool upgrade fcrawl` to get the
new skill text; other hooks and settings are never touched. `--uninstall`
removes exactly what `setup` added.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Optional

import click

from ..utils.output import console

GATE_NAME = "fcrawl_gate.py"
SKILL_NAME = "fcrawl"
BACKUP_SUFFIX = ".bak-fcrawl-setup"
CLAUDE_MD_BEGIN = "<!-- fcrawl-setup:begin (managed by `fcrawl setup claude-code`; edits inside are overwritten) -->"
CLAUDE_MD_END = "<!-- fcrawl-setup:end -->"
CLAUDE_MD_HEADING = "## fcrawl"
CLAUDE_MD_SECTION = f"""
{CLAUDE_MD_BEGIN}
## fcrawl — all web access (search, pages, docs, YouTube, Reddit, X, Dcard)

**Never use the built-in WebSearch / WebFetch or any Firecrawl MCP. All web work goes through the `fcrawl` CLI in Bash.** The full usage guide is the `fcrawl` skill — load it with the Skill tool (`skill: fcrawl`) before any web task; a hook denies WebSearch/WebFetch in any agent that hasn't loaded it yet (subagents load it themselves). The skill holds the hard rules (search results stay in Bash output, `scrape --save-temp`, never `--no-links` on docs, read docs pages fully), every subcommand, and the recipe for navigating API docs (`llms.txt` → `.md` pages).

Usually don't `cat` files in Bash — use the Read tool.
{CLAUDE_MD_END}
"""

# (event, matcher) -> the gate runs there. Mirrors Arthur's settings.json.
HOOK_SLOTS: tuple[tuple[str, str], ...] = (
    ("PreToolUse", "WebSearch|WebFetch"),
    ("PreToolUse", "Bash|PowerShell"),  # Windows Claude Code has a separate PowerShell tool
    ("PostToolUse", "Skill"),
    ("SessionStart", "compact"),
)
# matchers earlier versions wrote for the same slot: an entry under one of these is
# ours too and is moved to the current matcher on re-run
LEGACY_MATCHERS: dict[tuple[str, str], tuple[str, ...]] = {("PreToolUse", "Bash|PowerShell"): ("Bash",)}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def asset(*parts: str) -> str:
    return resources.files("fcrawl").joinpath("assets", "claude", *parts).read_text(encoding="utf-8")


def python_for_hooks() -> str:
    """The interpreter the hook command uses. A system python (macOS /usr/bin/python3,
    Linux /usr/bin/python3) is preferred over a venv/uv interpreter: hooks run outside
    any project and the gate only needs the standard library. If this process runs on
    a system python already, use that exact path."""
    if sys.platform == "win32":
        # No `python3` on Windows (Git Bash has none either). Prefer the py launcher,
        # then `python`: a command word, not a path with backslashes to quote.
        if shutil.which("py"):
            return "py -3"
        return "python"
    exe = Path(sys.executable).resolve()
    system_prefixes = ("/usr/bin/", "/usr/local/bin/", "/opt/homebrew/bin/", "/bin/")
    if str(exe).startswith(system_prefixes) and ".venv" not in str(exe) and "/uv/" not in str(exe):
        return str(exe)
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"):
        if Path(candidate).exists():
            return candidate
    return shutil.which("python3") or "python3"


def hook_shell_path(path: Path) -> str:
    """How the gate's path is written in the hook command. Claude Code runs hook commands
    through Git Bash on Windows (not cmd.exe), where an unquoted backslash is an escape:
    `C:\\Users\\x\\.claude\\hooks\\fcrawl_gate.py` became `Usersx.claudehooksfcrawl_gate.py`
    (exit 2 → every Bash call blocked). Forward slashes are what every shell and
    Python on Windows accept, so the command is `py -3 C:/Users/x/.claude/hooks/fcrawl_gate.py`."""
    return path.as_posix() if (sys.platform == "win32" or "\\" in str(path)) else str(path)


def gate_command(gate_path: Path, python: str) -> str:
    """POSIX-quoted (hooks run in sh / Git Bash on every platform), so a config dir with
    a space still runs. `python` may be a command with flags (`py -3`): not quoted as
    one word."""
    return f"{python} {shlex.quote(hook_shell_path(gate_path))}"


def _normalise_script(word: str) -> str:
    """A script path as Git Bash would hand it to Python, normalised for comparison:
    quotes stripped, backslashes as slashes, case-insensitive on Windows."""
    w = word.strip().strip("'\"")
    w = w.replace("\\", "/")
    return w.lower() if sys.platform == "win32" else w


def hook_script(hook: Any) -> str | None:
    """The script a command hook runs: last word of `<python> <script>` (quotes removed,
    slashes normalised), or None. Understands the three forms setup has ever written:
    `py -3 C:/x/fcrawl_gate.py` (current), `py -3 'C:\\x\\fcrawl_gate.py'` and the bare
    backslash form `py -3 C:\\x\\fcrawl_gate.py` (both legacy; the bare one is what Git
    Bash mangled), so a re-run replaces them instead of leaving stale entries."""
    if not isinstance(hook, dict) or hook.get("type") != "command" or not isinstance(hook.get("command"), str):
        return None
    cmd = hook["command"].strip()
    if not cmd:
        return None
    try:
        # posix=False keeps backslashes (a legacy unquoted Windows path would otherwise
        # be eaten exactly as Git Bash eats it); quotes are stripped by _normalise_script
        words = shlex.split(cmd, posix=False)
    except ValueError:
        return None
    return _normalise_script(words[-1]) if words else None


def is_gate_hook(hook: Any, gate_path: Path) -> bool:
    """Ours = runs exactly the gate file setup installs (another fcrawl_gate.py elsewhere
    on the machine is somebody else's), in any of the forms setup has written."""
    return hook_script(hook) == _normalise_script(str(gate_path))


class Plan:
    """What setup would change; `apply()` does it. Keeps dry-run and real runs on one path."""

    def __init__(self, base: Path, python: str):
        self.base = base
        self.python = python
        self.skill_path = base / "skills" / SKILL_NAME / "SKILL.md"
        self.gate_path = base / "hooks" / GATE_NAME
        self.settings_path = base / "settings.json"
        self.claude_md = base / "CLAUDE.md"
        self.actions: list[str] = []
        self.skill_text = asset("skills", "fcrawl", "SKILL.md")
        self.gate_text = asset("hooks", GATE_NAME)

    # --- install -------------------------------------------------------------

    def plan_install(self) -> None:
        if self._same(self.skill_path, self.skill_text):
            self.actions.append(f"skill      up to date: {self.skill_path}")
        else:
            self.actions.append(f"skill      {'update' if self.skill_path.exists() else 'write'}: {self.skill_path}")
        if self._same(self.gate_path, self.gate_text) and os.access(self.gate_path, os.X_OK):
            self.actions.append(f"hook       up to date: {self.gate_path}")
        else:
            self.actions.append(f"hook       {'update' if self.gate_path.exists() else 'write'}: {self.gate_path} (chmod +x)")
        settings, err = self._load_settings()
        if err:
            self.actions.append(f"settings   ERROR: {err}")
            return
        new_settings, added, updated = merge_hooks(settings, self.gate_path, gate_command(self.gate_path, self.python))
        if added or updated:
            what = []
            if added:
                what.append(f"add {added} hook entr{'y' if added == 1 else 'ies'}")
            if updated:
                what.append(f"update {updated} command{'' if updated == 1 else 's'}")
            self.actions.append(f"settings   {', '.join(what)} in {self.settings_path}"
                                f" (backup: {self.settings_path.name}{BACKUP_SUFFIX})")
        else:
            self.actions.append(f"settings   hooks already present in {self.settings_path}")
        self._new_settings = new_settings
        self._settings_changed = (added + updated) > 0
        if self._has_section():
            self.actions.append(f"CLAUDE.md  '{CLAUDE_MD_HEADING}' section already present in {self.claude_md}")
        else:
            self.actions.append(f"CLAUDE.md  append '{CLAUDE_MD_HEADING}' section to {self.claude_md}")

    def apply_install(self) -> None:
        self.skill_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._same(self.skill_path, self.skill_text):
            self.skill_path.write_text(self.skill_text, encoding="utf-8")
        self.gate_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._same(self.gate_path, self.gate_text):
            self.gate_path.write_text(self.gate_text, encoding="utf-8")
        self.gate_path.chmod(self.gate_path.stat().st_mode | 0o111)
        if getattr(self, "_settings_changed", False):
            self._write_settings(self._new_settings)
        if not self._has_section():
            self.claude_md.parent.mkdir(parents=True, exist_ok=True)
            existing = self.claude_md.read_text(encoding="utf-8") if self.claude_md.exists() else ""
            sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            self.claude_md.write_text(existing + sep + CLAUDE_MD_SECTION.lstrip("\n"), encoding="utf-8")

    # --- uninstall -----------------------------------------------------------

    def plan_uninstall(self) -> None:
        self.actions.append(f"skill      {'remove' if self.skill_path.exists() else 'absent'}: {self.skill_path}")
        self.actions.append(f"hook       {'remove' if self.gate_path.exists() else 'absent'}: {self.gate_path}")
        settings, err = self._load_settings()
        if err:
            self.actions.append(f"settings   ERROR: {err}")
            return
        new_settings, removed = remove_hooks(settings, self.gate_path)
        self.actions.append(f"settings   remove {removed} gate hook entr{'y' if removed == 1 else 'ies'}"
                            if removed else "settings   no gate hooks present")
        self._new_settings, self._settings_changed = new_settings, removed > 0
        self.actions.append(f"CLAUDE.md  {'remove' if self._has_section() else 'no'} '{CLAUDE_MD_HEADING}' section")

    def apply_uninstall(self) -> None:
        if self.skill_path.exists():
            self.skill_path.unlink()
            try:
                self.skill_path.parent.rmdir()  # only when empty
            except OSError:
                pass
        if self.gate_path.exists():
            self.gate_path.unlink()
        if getattr(self, "_settings_changed", False):
            self._write_settings(self._new_settings)
        if self._has_section():
            text = self.claude_md.read_text(encoding="utf-8")
            self.claude_md.write_text(remove_section(text), encoding="utf-8")

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _same(path: Path, text: str) -> bool:
        try:
            return path.read_text(encoding="utf-8") == text
        except OSError:
            return False

    def _load_settings(self) -> tuple[dict, Optional[str]]:
        if not self.settings_path.exists():
            return {}, None
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}, f"{self.settings_path} is not valid JSON ({exc}); fix it by hand first"
        if not isinstance(data, dict):
            return {}, f"{self.settings_path} must contain a JSON object"
        return data, None

    def _write_settings(self, data: dict) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        if self.settings_path.exists():
            shutil.copy2(self.settings_path, self.settings_path.with_name(self.settings_path.name + BACKUP_SUFFIX))
        tmp = self.settings_path.with_name(self.settings_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.settings_path)

    def _has_section(self) -> bool:
        """Only a block between our own markers counts; a user's '## fcrawl notes' is theirs."""
        if not self.claude_md.exists():
            return False
        text = self.claude_md.read_text(encoding="utf-8")
        return CLAUDE_MD_BEGIN in text and CLAUDE_MD_END in text


def merge_hooks(settings: dict, gate_path: Path, command: str) -> tuple[dict, int, int]:
    """Add the gate to each (event, matcher) slot that does not already run it; an entry
    that runs our gate with a different command line (older interpreter) is updated.
    Other hooks, other matchers and every other setting are left exactly as they are.
    Returns (settings, added, updated)."""
    out = json.loads(json.dumps(settings))  # deep copy
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise click.ClickException("settings.json: 'hooks' is not an object")
    added = updated = 0
    for event, matcher in HOOK_SLOTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise click.ClickException(f"settings.json: hooks.{event} is not a list")
        # an entry of ours under a matcher an older setup wrote (e.g. "Bash" before the
        # PowerShell tool existed) is removed from there and re-added under the current one
        for legacy in LEGACY_MATCHERS.get((event, matcher), ()):
            for g in groups:
                if isinstance(g, dict) and g.get("matcher") == legacy and isinstance(g.get("hooks"), list):
                    before = len(g["hooks"])
                    g["hooks"] = [h for h in g["hooks"] if not is_gate_hook(h, gate_path)]
                    updated += before - len(g["hooks"])
            groups[:] = [g for g in groups if not (isinstance(g, dict) and g.get("matcher") == legacy
                                                   and isinstance(g.get("hooks"), list) and not g["hooks"])]
        ours = [h for g in groups if isinstance(g, dict) and g.get("matcher") == matcher
                for h in (g.get("hooks") or []) if is_gate_hook(h, gate_path)]
        if ours:
            for h in ours:
                if h.get("command") != command:
                    h["command"] = command
                    updated += 1
            continue
        target = next((g for g in groups if isinstance(g, dict) and g.get("matcher") == matcher), None)
        entry = {"type": "command", "command": command}
        if target is None:
            groups.append({"matcher": matcher, "hooks": [entry]})
        else:
            target.setdefault("hooks", []).append(entry)
        added += 1
    return out, added, updated


def remove_hooks(settings: dict, gate_path: Path) -> tuple[dict, int]:
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out, 0
    removed = 0
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                kept_groups.append(g)
                continue
            kept = [h for h in g["hooks"] if not is_gate_hook(h, gate_path)]
            removed += len(g["hooks"]) - len(kept)
            if kept or not g["hooks"]:
                kept_groups.append(dict(g, hooks=kept))
            # a matcher group that only held the gate disappears with it
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return out, removed


def remove_section(text: str) -> str:
    """Drop exactly the block between our markers (and one blank line before it, if that
    blank line was ours). Anything the user wrote, including their own '## fcrawl …'
    headings, stays."""
    start = text.find(CLAUDE_MD_BEGIN)
    end = text.find(CLAUDE_MD_END, start)
    if start < 0 or end < 0:
        return text
    end += len(CLAUDE_MD_END)
    if text[end:end + 1] == "\n":
        end += 1
    before = text[:start]
    if before.endswith("\n\n"):
        before = before[:-1]
    return before + text[end:]


def ensure_pythonutf8_windows(dry_run: bool) -> str | None:
    """Windows only: set the user-level PYTHONUTF8=1 (belt and braces with the in-process
    UTF-8 fixes: it also covers the hook and any Python the agent runs). Idempotent.
    Returns a one-line status for the plan, None off Windows."""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return "env        PYTHONUTF8: winreg not available; set user-level PYTHONUTF8=1 by hand"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                current, _ = winreg.QueryValueEx(key, "PYTHONUTF8")
            except FileNotFoundError:
                current = None
    except OSError as exc:
        return f"env        PYTHONUTF8: could not read the user environment ({exc})"
    if str(current) == "1":
        return "env        PYTHONUTF8=1 already set (user level)"
    if dry_run:
        return "env        set user-level PYTHONUTF8=1 (setx)"
    try:
        subprocess.run(["setx", "PYTHONUTF8", "1"], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"env        PYTHONUTF8: setx failed ({exc}); set it by hand in System Properties"
    return "env        set user-level PYTHONUTF8=1; fully quit and restart the Claude app to pick it up"


DOCTOR_PAYLOAD = {"hook_event_name": "PreToolUse", "tool_name": "WebSearch", "tool_input": {"query": "x"},
                  "session_id": "fcrawl-doctor", "transcript_path": "/nonexistent-fcrawl-doctor"}


def shell_for_hooks() -> list[str] | None:
    """The shell Claude Code runs hook commands in: sh/bash on Unix, Git Bash on Windows.
    None when no bash can be found (then the doctor falls back to PowerShell)."""
    if sys.platform != "win32":
        return ["/bin/sh", "-c"]
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files\Git\usr\bin\bash.exe"):
        if candidate and Path(candidate).exists():
            return [candidate, "-c"]
    return None


def run_hook_command(command: str, payload: dict) -> tuple[int, str, str, str]:
    """Run the exact command string from settings.json the way Claude Code does (through
    the shell), feeding `payload` on stdin. Returns (exit code, stdout, stderr, how)."""
    data = json.dumps(payload).encode("utf-8")
    shell = shell_for_hooks()
    if shell is not None:
        argv, how = shell + [command], f"{Path(shell[0]).name} -c"
    else:
        argv, how = ["powershell", "-NoProfile", "-Command", command], "powershell -Command"
    try:
        r = subprocess.run(argv, input=data, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc), how
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"), how


def run_doctor(base: Path) -> int:
    """Check an installed integration end to end; returns the number of problems."""
    problems = 0
    gate = base / "hooks" / GATE_NAME
    skill = base / "skills" / SKILL_NAME / "SKILL.md"
    settings_path = base / "settings.json"
    console.print(f"[bold]fcrawl ↔ Claude Code doctor ({base})[/bold]")

    def ok(msg: str) -> None:
        console.print(f"  [green]ok[/green]    {msg}", markup=True, highlight=False)

    def bad(msg: str) -> None:
        nonlocal problems
        problems += 1
        console.print(f"  [red]FAIL[/red]  {msg}", markup=True, highlight=False)

    if skill.exists():
        try:
            text = skill.read_text(encoding="utf-8")
            ok(f"skill at {skill} ({len(text.splitlines())} lines)" if "## Hard rules" in text
               else f"skill at {skill} but it has no '## Hard rules' section")
        except (OSError, ValueError) as exc:
            bad(f"skill at {skill} cannot be read as UTF-8: {exc}")
    else:
        bad(f"skill missing: {skill} (run `fcrawl setup claude-code`)")
    if gate.exists():
        ok(f"hook file at {gate}")
    else:
        bad(f"hook file missing: {gate} (run `fcrawl setup claude-code`)")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except (OSError, ValueError) as exc:
        bad(f"{settings_path} unreadable: {exc}")
        settings = {}
    commands: dict[tuple[str, str], str] = {}
    for ev, groups in (settings.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                if is_gate_hook(h, gate):
                    commands[(ev, g.get("matcher"))] = h["command"]
    for slot in HOOK_SLOTS:
        if slot in commands:
            ok(f"settings: {slot[0]}[{slot[1]}] -> {commands[slot]}")
        else:
            bad(f"settings: no gate entry for {slot[0]}[{slot[1]}]")
    stale = [(k, v) for k, v in commands.items() if k not in HOOK_SLOTS]
    for k, v in stale:
        bad(f"settings: stale gate entry {k[0]}[{k[1]}] (re-run `fcrawl setup claude-code` to migrate)")
    if commands:
        command = next(iter(commands.values()))
        words = command.split()
        interp = shutil.which(words[0]) if words else None
        if interp:
            ok(f"interpreter `{words[0]}` -> {interp}")
        else:
            bad(f"interpreter `{words[0] if words else '?'}` not found on PATH")
        code, out, err, how = run_hook_command(command, DOCTOR_PAYLOAD)
        decision = None
        try:
            decision = json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision")
        except (ValueError, AttributeError):
            pass
        if code == 0 and decision == "deny":
            ok(f"hook run ({how}): exit 0, WebSearch denied before the skill is loaded (expected)")
        elif code == 0:
            bad(f"hook run ({how}): exit 0 but no deny decision (stdout: {out[:120]!r})")
        else:
            bad(f"hook run ({how}): exit {code} — Claude Code would block the tool call. stderr: {err.strip()[:200]!r}")
    if sys.platform == "win32":
        env = os.environ.get("PYTHONUTF8")
        (ok if env == "1" else bad)(f"PYTHONUTF8={env!r} in this process"
                                    + ("" if env == "1" else " (set user-level PYTHONUTF8=1 and restart the Claude app)"))
    console.print("[green]All good.[/green]" if not problems else f"[red]{problems} problem(s).[/red]")
    return problems


@click.group()
def setup():
    """Install integrations for a new user (Claude Code hooks + skill)."""


@setup.command("claude-code")
@click.option("--dry-run", is_flag=True, help="Show what would change, touch nothing")
@click.option("--uninstall", is_flag=True, help="Remove the skill, the gate hook, its settings entries and the CLAUDE.md section")
@click.option("--python", "python_path", default=None,
              help="Interpreter for the hook command (default: a system python3; the gate needs only the stdlib)")
@click.option("--doctor", is_flag=True, help="Check an installed integration: files, settings entries, interpreter, "
                                             "and run the hook once exactly as Claude Code would")
def setup_claude_code(dry_run: bool, uninstall: bool, python_path: Optional[str], doctor: bool):
    """Set up Claude Code to use fcrawl for all web access.

    \b
    Installs, idempotently:
      ~/.claude/skills/fcrawl/SKILL.md   the fcrawl skill
      ~/.claude/hooks/fcrawl_gate.py     the gate hook (chmod +x)
      ~/.claude/settings.json            4 hook entries (merged; backup kept)
      ~/.claude/CLAUDE.md                a short "## fcrawl" section

    Re-run after upgrading fcrawl to refresh the skill text. Nothing else in
    settings.json or CLAUDE.md is touched. CLAUDE_CONFIG_DIR is honoured.
    """
    base = claude_dir()
    if doctor:
        raise SystemExit(1 if run_doctor(base) else 0)
    plan = Plan(base, python_path or python_for_hooks())
    if uninstall:
        plan.plan_uninstall()
    else:
        plan.plan_install()
    console.print(f"[bold]{'Would ' if dry_run else ''}{'remove' if uninstall else 'install'} fcrawl ↔ Claude Code in {base}[/bold]")
    for a in plan.actions:
        console.print(f"  {a}", markup=False, highlight=False)
    if any(a.split()[1:2] == ["ERROR:"] for a in plan.actions):
        raise click.Abort()
    env_line = None if uninstall else ensure_pythonutf8_windows(dry_run)
    if env_line:
        console.print(f"  {env_line}", markup=False, highlight=False)
    if dry_run:
        return
    if uninstall:
        plan.apply_uninstall()
        console.print("[green]Removed.[/green] Restart Claude Code for the hooks to go away.")
        return
    plan.apply_install()
    console.print("[green]Done.[/green]")
    console.print("Next:")
    console.print("  1. Restart Claude Code (hooks are read at startup).")
    console.print("  2. Make sure ~/.fcrawlrc has your gateway: "
                  '{"api_url": "https://<gateway>", "gateway_token": "fgw_<name>_<token>"}')
    console.print("  3. In Claude Code, ask for a web search: it should load the fcrawl skill and run `fcrawl search`.")
    console.print("  Check it: fcrawl setup claude-code --doctor")
