"""Source contracts for static operator assets."""

from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]


def test_operator_ui_links_a_valid_svg_favicon() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    favicon = ROOT / "frontend" / "favicon.svg"

    assert '<link rel="icon" href="favicon.svg" type="image/svg+xml">' in html
    root = ElementTree.parse(favicon).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.attrib["viewBox"] == "0 0 64 64"


def test_operator_ui_shows_the_deployed_release() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert 'id="version-badge"' in html
    assert "health.version" in script
    assert "deployed release" in script
