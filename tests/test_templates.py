"""Whether the pages will render at all.

A missing endif breaks every page, not just the one edited, and an unbalanced
<div> renders without error and lays the page out wrongly -- which is harder to
notice than a template that refuses to load. Both have happened here more than
once. These lived in test_jobs.py, where nobody editing a template would ever
find them.
"""
from __future__ import annotations

import re
from pathlib import Path


def test_every_template_balances_its_blocks():
    """A missing endif breaks every page, not just the one edited.

    This has caught the same mistake twice while adding dashboard sections,
    both times only because it was run by hand. Run it here instead.
    """
    unbalanced = []
    for path in Path("app/web/templates").rglob("*.html"):
        source = path.read_text()
        for tag, closing in (("if", "endif"), ("for", "endfor"),
                             ("block", "endblock")):
            opens = len(re.findall(r"\{%-?\s*" + tag + r"\b", source))
            closes = len(re.findall(r"\{%-?\s*" + closing + r"\b", source))
            if opens != closes:
                unbalanced.append(f"{path.name}: {tag} {opens} vs {closes}")
    assert unbalanced == [], unbalanced


def test_every_template_balances_its_html_tags():
    """Jinja blocks are not the only thing I have left unclosed.

    An unbalanced table or div renders without error and lays the page out
    wrongly, which is harder to notice than a template that refuses to load.
    """
    unbalanced = []
    for path in Path("app/web/templates").rglob("*.html"):
        source = path.read_text()
        for tag in ("table", "tr", "td", "th", "form", "div", "select"):
            opens = len(re.findall(r"<" + tag + r"[ >]", source))
            closes = len(re.findall(r"</" + tag + r">", source))
            if opens != closes:
                unbalanced.append(f"{path.name}: <{tag}> {opens} vs {closes}")
    assert unbalanced == [], unbalanced


def test_the_template_check_looks_at_real_templates():
    """Guards the guard: an empty glob would pass silently."""
    assert len(list(Path("app/web/templates").rglob("*.html"))) > 10
