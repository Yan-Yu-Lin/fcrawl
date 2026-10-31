"""`fcrawl setup claude-code` against a temporary Claude config dir."""

from __future__ import annotations

import json
import os
import stat

import pytest
from click.testing import CliRunner

from fcrawl.cli import cli
from fcrawl.commands import setup as su


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    d = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


def run(*args):
    return CliRunner().invoke(cli, ["setup", "claude-code", *args])


def gate_cmds(settings: dict, cdir) -> list[tuple[str, str]]:
    gate = cdir / "hooks" / "fcrawl_gate.py"
    out = []
    for ev, groups in settings.get("hooks", {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if su.is_gate_hook(h, gate):
                    out.append((ev, g.get("matcher")))
    return sorted(out)


ARTHUR_LIKE = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git:*)"]},
    "hooks": {
        "PostToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/python3 /x/logger.py"}]},
            {"matcher": "Read|Bash", "hooks": [{"type": "command", "command": "uv run memory.py", "timeout": 15}]},
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command", "command": "/x/test_hook.sh"}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/other_bash_hook.py"}]},
        ],
        "SessionStart": [
            {"matcher": "compact", "hooks": [{"type": "command", "command": "bash /x/herdr.sh"}]},
        ],
    },
}


def test_fresh_install_dry_run_then_install(cdir):
    out = run("--dry-run")
    assert out.exit_code == 0, out.output
    assert not cdir.exists()  # dry run touches nothing
    assert "Would install" in out.output and "add 4 hook entries" in out.output
    out = run()
    assert out.exit_code == 0, out.output
    skill = cdir / "skills" / "fcrawl" / "SKILL.md"
    gate = cdir / "hooks" / "fcrawl_gate.py"
    assert skill.read_text() == su.asset("skills", "fcrawl", "SKILL.md")
    assert skill.read_text().startswith("---\nname: fcrawl")
    assert gate.read_text() == su.asset("hooks", "fcrawl_gate.py")
    assert gate.stat().st_mode & stat.S_IXUSR
    settings = json.loads((cdir / "settings.json").read_text())
    assert gate_cmds(settings, cdir) == [("PostToolUse", "Skill"), ("PreToolUse", "Bash"),
                                   ("PreToolUse", "WebSearch|WebFetch"), ("SessionStart", "compact")]
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.endswith(f" {gate}") and cmd.split()[0].endswith("python3")
    assert not (cdir / "settings.json.bak-fcrawl-setup").exists()  # nothing to back up on a fresh dir
    md = (cdir / "CLAUDE.md").read_text()
    assert md.startswith(su.CLAUDE_MD_BEGIN) and "## fcrawl" in md and "fcrawl` CLI in Bash" in md
    assert md.rstrip().endswith(su.CLAUDE_MD_END)
    assert "Restart Claude Code" in out.output and ".fcrawlrc" in out.output


def test_merge_keeps_other_hooks_and_settings(cdir):
    cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps(ARTHUR_LIKE, indent=2))
    (cdir / "CLAUDE.md").write_text("# mine\n\nkeep this\n")
    out = run()
    assert out.exit_code == 0, out.output
    settings = json.loads((cdir / "settings.json").read_text())
    assert settings["model"] == "opus" and settings["permissions"] == {"allow": ["Bash(git:*)"]}
    # every original hook is still there, in place
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0] == ARTHUR_LIKE["hooks"]["PreToolUse"][0]
    assert pre[1]["matcher"] == "Bash" and pre[1]["hooks"][0]["command"] == "/x/other_bash_hook.py"
    gate = cdir / "hooks" / "fcrawl_gate.py"
    assert su.is_gate_hook(pre[1]["hooks"][1], gate)  # appended to the existing Bash group
    assert pre[2]["matcher"] == "WebSearch|WebFetch" and su.is_gate_hook(pre[2]["hooks"][0], gate)
    ss = settings["hooks"]["SessionStart"][0]
    assert ss["matcher"] == "compact" and ss["hooks"][0]["command"] == "bash /x/herdr.sh" and su.is_gate_hook(ss["hooks"][1], gate)
    post = settings["hooks"]["PostToolUse"]
    assert post[0] == ARTHUR_LIKE["hooks"]["PostToolUse"][0] and post[1]["hooks"][0]["timeout"] == 15
    assert post[2]["matcher"] == "Skill"
    assert gate_cmds(settings, cdir) == [("PostToolUse", "Skill"), ("PreToolUse", "Bash"),
                                   ("PreToolUse", "WebSearch|WebFetch"), ("SessionStart", "compact")]
    # backup of the pre-setup file
    assert json.loads((cdir / "settings.json.bak-fcrawl-setup").read_text()) == ARTHUR_LIKE
    md = (cdir / "CLAUDE.md").read_text()
    assert md.startswith("# mine\n\nkeep this\n") and "## fcrawl" in md


def test_idempotent_rerun_and_skill_refresh(cdir):
    assert run().exit_code == 0
    first = json.loads((cdir / "settings.json").read_text())
    md = (cdir / "CLAUDE.md").read_text()
    out = run()
    assert out.exit_code == 0 and "already present" in out.output and "up to date" in out.output
    assert json.loads((cdir / "settings.json").read_text()) == first
    assert (cdir / "CLAUDE.md").read_text() == md  # not appended twice
    assert not (cdir / "settings.json.bak-fcrawl-setup").exists()  # no write, no backup
    # an edited (older) skill file gets refreshed, the gate too; settings untouched
    (cdir / "skills" / "fcrawl" / "SKILL.md").write_text("old")
    (cdir / "hooks" / "fcrawl_gate.py").write_text("old")
    out = run()
    assert "update:" in out.output
    assert (cdir / "skills" / "fcrawl" / "SKILL.md").read_text() == su.asset("skills", "fcrawl", "SKILL.md")
    assert (cdir / "hooks" / "fcrawl_gate.py").read_text() == su.asset("hooks", "fcrawl_gate.py")
    assert json.loads((cdir / "settings.json").read_text()) == first


def test_uninstall_removes_only_ours(cdir):
    cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps(ARTHUR_LIKE))
    (cdir / "CLAUDE.md").write_text("# mine\n\nkeep this\n\n## Other\n\nalso keep\n")
    assert run().exit_code == 0
    out = run("--uninstall", "--dry-run")
    assert out.exit_code == 0 and "remove 4 gate hook entries" in out.output
    assert (cdir / "hooks" / "fcrawl_gate.py").exists()
    out = run("--uninstall")
    assert out.exit_code == 0, out.output
    assert not (cdir / "skills" / "fcrawl").exists() and not (cdir / "hooks" / "fcrawl_gate.py").exists()
    settings = json.loads((cdir / "settings.json").read_text())
    assert gate_cmds(settings, cdir) == []
    # the other hooks survive exactly; the gate-only groups are gone
    assert settings["hooks"]["PreToolUse"] == ARTHUR_LIKE["hooks"]["PreToolUse"]
    assert settings["hooks"]["SessionStart"] == ARTHUR_LIKE["hooks"]["SessionStart"]
    assert settings["hooks"]["PostToolUse"] == ARTHUR_LIKE["hooks"]["PostToolUse"]
    assert settings["model"] == "opus"
    md = (cdir / "CLAUDE.md").read_text()
    assert "## fcrawl" not in md and "keep this" in md and "## Other\n\nalso keep" in md
    # a second uninstall is a no-op
    out = run("--uninstall")
    assert out.exit_code == 0 and "no gate hooks present" in out.output


def test_invalid_settings_json_aborts_before_touching_anything(cdir):
    cdir.mkdir()
    (cdir / "settings.json").write_text("{not json")
    out = run()
    assert out.exit_code != 0 and "not valid JSON" in out.output
    assert not (cdir / "skills").exists() and not (cdir / "hooks").exists()
    assert (cdir / "settings.json").read_text() == "{not json"


def test_python_choice_and_override(cdir, monkeypatch):
    out = run("--python", "/custom/python3")
    assert out.exit_code == 0
    settings = json.loads((cdir / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].startswith("/custom/python3 ")
    # the default is a system python, never a venv interpreter
    monkeypatch.setattr(su.sys, "executable", "/home/u/proj/.venv/bin/python")
    assert ".venv" not in su.python_for_hooks()
    monkeypatch.setattr(su.sys, "executable", "/usr/bin/python3")
    assert su.python_for_hooks() == "/usr/bin/python3" or not os.path.exists("/usr/bin/python3")


def test_remove_section_helper():
    block = su.CLAUDE_MD_SECTION.lstrip("\n")
    text = "# A\n\n" + block + "\n## B\n\nkeep\n"
    assert su.remove_section(text) == "# A\n\n## B\n\nkeep\n"
    assert su.remove_section("## fcrawl notes\nmine\n") == "## fcrawl notes\nmine\n"  # the user's, untouched
    assert su.remove_section("x\n") == "x\n"


def test_r1_user_fcrawl_heading_is_not_ours(cdir):
    """#1: a user's own '## fcrawl notes' section is neither 'already installed' nor removed."""
    cdir.mkdir()
    mine = "# Personal\n\n## fcrawl notes\n\nmy own instructions\n"
    (cdir / "CLAUDE.md").write_text(mine)
    out = run()
    assert out.exit_code == 0 and "append" in out.output
    md = (cdir / "CLAUDE.md").read_text()
    assert md.startswith(mine) and su.CLAUDE_MD_BEGIN in md
    assert run("--uninstall").exit_code == 0
    assert (cdir / "CLAUDE.md").read_text() == mine


def test_r1_paths_with_spaces_are_quoted(tmp_path, monkeypatch):
    """#5: a config dir with a space gives a hook command that still runs."""
    import subprocess
    d = tmp_path / "claude config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    assert run().exit_code == 0
    cmd = json.loads((d / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "'" in cmd or '"' in cmd
    r = subprocess.run(cmd, shell=True, input=b'{"hook_event_name":"PreToolUse","tool_name":"Read"}', capture_output=True)
    assert r.returncode == 0, r.stderr
    # ownership parsing handles the quoted command: idempotent, uninstall finds it
    out = run()
    assert "already present" in out.output
    assert run("--uninstall").exit_code == 0
    assert json.loads((d / "settings.json").read_text())["hooks"] == {}


def test_r1_hook_honours_claude_config_dir(cdir, tmp_path, monkeypatch):
    """#6: the installed gate reads the skill and writes its state under CLAUDE_CONFIG_DIR."""
    import subprocess
    assert run().exit_code == 0
    gate = cdir / "hooks" / "fcrawl_gate.py"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home), "CLAUDE_CONFIG_DIR": str(cdir)}
    r = subprocess.run([su.python_for_hooks(), str(gate)], input=b'{"hook_event_name":"SessionStart","source":"compact"}',
                       capture_output=True, env=env)
    assert r.returncode == 0 and b"Hard rules" in r.stdout, r.stderr
    r = subprocess.run([su.python_for_hooks(), str(gate)], env=env, capture_output=True,
                       input=b'{"hook_event_name":"PostToolUse","tool_name":"Skill","tool_input":{"skill":"fcrawl"},'
                             b'"tool_response":{"success":true},"session_id":"s1"}')
    assert r.returncode == 0
    assert (cdir / "state" / "fcrawl-gate" / "s1__main").exists()
    assert not (fake_home / ".claude").exists()


def test_r1_ownership_is_the_exact_path_and_interpreter_updates(cdir):
    """#7: another fcrawl_gate.py elsewhere is not ours; re-running with --python rewrites
    our four commands and nothing else."""
    cdir.mkdir()
    other = {"type": "command", "command": "/usr/bin/python3 /opt/team/fcrawl_gate.py"}
    (cdir / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}}))
    assert run().exit_code == 0
    settings = json.loads((cdir / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0] == other
    assert len(gate_cmds(settings, cdir)) == 4
    out = run("--python", "/custom/python3")
    assert out.exit_code == 0 and "update 4 commands" in out.output
    settings = json.loads((cdir / "settings.json").read_text())
    gate = str(cdir / "hooks" / "fcrawl_gate.py")
    ours = [h["command"] for ev in settings["hooks"].values() for g in ev for h in g["hooks"] if su.hook_script(h) == gate]
    assert len(ours) == 4 and all(c.startswith("/custom/python3 ") for c in ours)
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0] == other
    assert run("--uninstall").exit_code == 0
    settings = json.loads((cdir / "settings.json").read_text())
    assert settings["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]}
