"""Static contracts for visualization controls in the operator UI."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_switch_uses_the_selected_channel_and_plain_language() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    api = (ROOT / "frontend" / "api.js").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

    assert "async function switchVisualization(plugin, instant) {\n  const name = state.selected;" in script
    assert "Apply preloaded set" in html
    assert "isolated visualization switching is" in html
    assert "not part of this release" in html
    assert "Apply hot set" not in html
    assert "hot set" not in html.lower()
    assert "Add that plugin to hot_set" not in script
    assert "toast('warn', 'not_in_hot_set'" not in script
    assert "disabled: missing || !enabled || (live && !isHot)" in script
    assert "api.setVisualization(name, plugin, !live)" in script
    assert "allow_restart: allowRestart" in api
