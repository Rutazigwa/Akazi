"""The agent definitions in .claude/agents are instructions, and instructions
rot.

An agent told to run `tests/test_templates_balance.py` when the test actually
lives elsewhere does not fail loudly -- it quietly skips the check and reports
success. That exact mistake was in the first draft of screen-reviewer.md and
this test is what found it.

So: every repository path an agent names must exist, and every test function
it names must be defined somewhere in tests/.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parent.parent / ".claude" / "agents"

# The tool names the harness actually offers. An agent granted a tool that does
# not exist silently loses that capability.
KNOWN_TOOLS = {"Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch"}

# Looks like a path into this repository rather than prose or a shell fragment.
LOOKS_LIKE_PATH = re.compile(
    r"^(app|tests|migrations|scripts|docs)/[\w./-]+$|^(CLAUDE|DEPLOYMENT|README)\.md$"
)


def agent_files() -> list[Path]:
    return sorted(p for p in AGENTS.glob("*.md") if p.name != "README.md")


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    block = text.split("---\n")[1]
    return dict(re.findall(r"^(\w+):\s*(.+)$", block, re.M))


def test_there_are_agents_to_check():
    # Guards the guard: every other test here iterates, and an empty directory
    # would make all of them pass while checking nothing.
    assert len(agent_files()) >= 5


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_frontmatter_is_complete_and_consistent(path: Path):
    keys = frontmatter(path)
    for required in ("name", "description", "tools", "model"):
        assert required in keys, f"{path.name} is missing '{required}'"

    assert keys["name"] == path.stem, (
        f"{path.name} declares name={keys['name']!r}; the harness addresses an "
        f"agent by filename, so these must match"
    )

    unknown = {t.strip() for t in keys["tools"].split(",")} - KNOWN_TOOLS
    assert not unknown, f"{path.name} grants unknown tools: {sorted(unknown)}"

    # The description is what the harness matches against to choose an agent.
    # One sentence of generality picks the wrong agent or none at all.
    assert len(keys["description"]) >= 120, (
        f"{path.name} description is too thin to route on"
    )


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_every_path_an_agent_names_exists(path: Path):
    root = AGENTS.parent.parent
    quoted = re.findall(r"`([^`]+)`", path.read_text())
    named = [q for q in quoted if LOOKS_LIKE_PATH.match(q)]
    assert named, f"{path.name} names no repository paths; is it specific enough?"

    missing = [q for q in named if not (root / q).exists()]
    assert not missing, f"{path.name} points at paths that do not exist: {missing}"


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_every_test_function_an_agent_names_is_defined(path: Path):
    tests_dir = AGENTS.parent.parent / "tests"
    defined = set()
    for f in tests_dir.glob("test_*.py"):
        defined.update(re.findall(r"^def (test_\w+)", f.read_text(), re.M))

    quoted = re.findall(r"`(test_\w+)`", path.read_text())
    missing = [q for q in quoted if q not in defined]
    assert not missing, f"{path.name} names undefined tests: {missing}"


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.stem)
def test_every_agent_says_how_to_demonstrate_a_finding(path: Path):
    """The one discipline every agent shares.

    A subagent that reports a hypothesis as a finding costs more than it saves:
    someone has to go and disprove it. Each definition must tell its agent to
    make the defect happen before reporting it.
    """
    body = path.read_text().lower()
    assert any(
        word in body
        for word in ("demonstrate", "measure", "reproduce", "run the suite", "screenshot")
    ), f"{path.name} never tells the agent to prove a finding before reporting it"
