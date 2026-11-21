"""A saved ``platform_toolsets`` list naming ``web_extract`` must enable it (#t_c221319a).

``web_extract`` is a single-tool toolset (read-only link reading for group-sandboxed profiles). It was in
``toolsets.TOOLSETS`` but not in the configurable registry, so ``_get_platform_tools`` treated it as an MCP
server name and silently dropped it: ``[memory, web_extract]`` resolved to ``{memory}`` at runtime and in
``hermes tools list``.
"""

from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _get_platform_tools


def test_explicit_web_extract_survives_resolution():
    config = {"platform_toolsets": {"whatsapp": ["memory", "web_extract"]}}
    enabled = _get_platform_tools(config, "whatsapp", include_default_mcp_servers=False)
    assert enabled == {"memory", "web_extract"}


def test_web_extract_is_a_listed_row():
    assert "web_extract" in {key for key, _, _ in CONFIGURABLE_TOOLSETS}


def test_tools_list_shows_web_extract_enabled(capsys):
    # The swap verifier reads `hermes tools list --platform whatsapp`; the row must exist and read enabled.
    from hermes_cli.tools_config_mcp import _print_tools_list

    _print_tools_list({"memory", "web_extract"}, {}, "whatsapp")
    out = capsys.readouterr().out
    assert any("enabled" in line and "web_extract" in line and "disabled" not in line for line in out.splitlines())


def test_composite_default_does_not_infer_web_extract():
    # Only an explicit save enables the narrow toolset; the platform composite keeps reporting ``web``.
    enabled = _get_platform_tools({}, "whatsapp", include_default_mcp_servers=False)
    assert "web_extract" not in enabled
    assert "web" in enabled


def test_explicit_web_without_web_extract_stays_as_saved():
    config = {"platform_toolsets": {"whatsapp": ["web"]}}
    enabled = _get_platform_tools(config, "whatsapp", include_default_mcp_servers=False)
    assert "web" in enabled and "web_extract" not in enabled
