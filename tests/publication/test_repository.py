import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"


def test_readme_title_and_summary_are_publishable():
    lines = README.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# ")
    assert len(lines[0][2:]) <= 64
    summary = next(line for line in lines[1:] if line.strip())
    assert len(re.sub(r"[*_`]", "", summary)) <= 160


def test_readme_has_required_sections():
    content = README.read_text(encoding="utf-8")
    required = [
        "## What This Is",
        "## Architecture",
        "## Technology Stack",
        "## Quick Start (Development)",
        "## Project Structure",
        "## License",
    ]
    positions = [content.index(section) for section in required]
    assert positions == sorted(positions)


def test_readme_local_links_resolve():
    content = README.read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", content)
    local_links = [link.split("#", 1)[0] for link in links if "://" not in link]
    missing = [link for link in local_links if link and not (ROOT / link).exists()]
    assert not missing, f"missing README targets: {missing}"


def test_readme_contains_no_template_repository_url():
    content = README.read_text(encoding="utf-8")
    assert "YOUR_ORG" not in content


def test_architecture_and_validation_docs_are_linked():
    content = README.read_text(encoding="utf-8")
    assert "docs/architecture.md" in content
    assert "tests/validation_matrix.yaml" in content
