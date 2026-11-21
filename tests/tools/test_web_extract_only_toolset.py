"""The ``web_extract`` toolset is read-only content extraction with no search tool (#t_c221319a)."""

from toolsets import TOOLSETS, get_toolset, resolve_toolset


def test_web_extract_toolset_exposes_only_extraction():
    ts = get_toolset("web_extract")
    assert ts is not None
    assert set(ts["tools"]) == {"web_extract"}


def test_web_extract_toolset_resolves_without_search():
    names = resolve_toolset("web_extract")
    assert "web_extract" in names
    assert "web_search" not in names


def test_web_extract_toolset_is_registered_under_its_own_name():
    assert "web_extract" in TOOLSETS
