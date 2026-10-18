r"""The tool must not import the dashboard — the whole split depends on it.

The pattern is deliberately wider than the obvious one: the plan's original
`discord_scraper\.(api|auth)` grep returned zero matches in the tool while five
files imported `discord_stats`, which is a false green.
"""

import re
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "src" / "discord_scraper"
PATTERN = re.compile(r"discord_stats|discord_scraper\.(api|auth)")


def test_the_tool_never_imports_the_dashboard() -> None:
    offenders: list[str] = []
    for path in sorted(TOOL.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if PATTERN.search(line):
                offenders.append(f"{path.relative_to(TOOL)}:{number}: {line.strip()}")
    assert not offenders, "the tool imports the dashboard:\n" + "\n".join(offenders)


def test_the_old_dashboard_modules_are_gone_from_the_tool() -> None:
    assert not (TOOL / "api").exists()
    assert not (TOOL / "auth").exists()


def test_the_dashboard_never_reaches_for_the_old_tool_api() -> None:
    dashboard = TOOL.parent / "discord_stats"
    pattern = re.compile(r"discord_scraper\.(api|auth)")
    offenders = [
        f"{path.relative_to(dashboard)}:{number}: {line.strip()}"
        for path in sorted(dashboard.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, "the dashboard imports the old tool api:\n" + "\n".join(offenders)
