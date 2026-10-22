from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "shared-ui" / "app.js"
STYLES = ROOT / "shared-ui" / "styles.css"
INDEX = ROOT / "shared-ui" / "index.html"


def test_sidebar_folder_tree_is_live_not_hardcoded():
    """The Open folder tree must be rendered from the API, not baked into HTML.

    M21: the selected folder should expand and display actual workspace files.
    """
    html = INDEX.read_text(encoding="utf-8")
    tree_start = html.index("data-folder-tree")
    tree_block = html[tree_start:tree_start + 400]

    assert "main.py" not in tree_block, "hardcoded placeholder entries still in folder tree markup"
    assert "private-workspace-path" not in tree_block, "hardcoded placeholder paths still in folder tree markup"


def test_sidebar_folder_tree_renders_and_expands_from_api():
    js = APP_JS.read_text(encoding="utf-8")

    assert "renderFolderTree" in js, "no renderer for the sidebar folder tree"
    assert "data-folder-tree" in js, "renderer never targets the sidebar tree container"
    # Selecting a folder must expand it, not merely highlight it.
    assert "expandedFolders" in js, "no expanded-folder state, so selection cannot auto-expand"


def test_duplicate_map_tab_tree_removed():
    """MAP tab tree duplicated the sidebar tree and was removed to avoid duplicate UI.

    Both used the same `/api/workspace/tree` data; the MAP tab keeps its mind-map preview.
    """
    html = INDEX.read_text(encoding="utf-8")
    assert "data-file-tree-shell" not in html, "duplicate MAP tab file tree still present"
    js = APP_JS.read_text(encoding="utf-8")
    assert "data-file-tree-body" not in js, "MAP tab tree renderer still wired up"


def test_quest_tab_uses_theme_variables_for_shell_and_rows():
    css = STYLES.read_text(encoding="utf-8")
    quest_start = css.index('.stage-view[data-view="quest"] .quest-shell')
    quest_css = css[quest_start:]

    assert "--quest-bg" in css
    assert "--quest-border" in css
    assert "--quest-accent" in css
    assert "--quest-text" in css
    assert "border: 1px solid var(--quest-border)" in quest_css
    assert "background: var(--quest-bg)" in quest_css
    assert "color: var(--quest-text)" in quest_css
    assert "box-shadow: inset 0 0 0 1px" in quest_css


def test_chat_slash_commands_are_handled_client_side_before_runtime():
    js = APP_JS.read_text(encoding="utf-8")

    assert "const CHAT_COMMANDS" in js
    assert "function handleSlashCommand" in js
    assert "case 'clear':" in js
    assert "case 'help':" in js
    assert "case 'quest':" in js
    assert "case 'map':" in js
    assert "case 'chat':" in js
    assert "case 'theme':" in js
    assert "if (handleSlashCommand(prompt))" in js


# ── M16: coding-tool UI contracts ────────────────────────────────────────

INDEX = ROOT / "shared-ui" / "index.html"


def test_agent_console_has_progress_and_diff_tabs():
    html = INDEX.read_text(encoding="utf-8")
    assert "data-progress-panel" in html
    assert "data-progress-box" in html
    assert "data-progress-badge" in html
    assert "data-stage-progress" in html
    # Diff is now a toolbox tab, not a standalone panel
    assert "data-console-activity-tab=\"diff\"" in html
    assert "data-thinking-tab-count" in html


def test_map_tab_has_no_duplicate_file_tree():
    """The MAP tab keeps its mind-map preview instead of duplicating the sidebar file tree."""
    html = INDEX.read_text(encoding="utf-8")
    assert "data-file-tree-shell" not in html
    assert "data-file-tree-body" not in html
    assert "data-map-preview" in html, "MAP tab lost its mind-map preview"


def test_app_js_wires_progress_diff_and_tree():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function renderProgressPanel" in js
    assert "function renderHeaderStatus" in js
    assert "function startLiveProgressPolling" in js
    assert "function stopLiveProgressPolling" in js
    assert "async function refreshDiffOutput" in js
    # M21: refreshFileTree (MAP tab) became renderFolderTree (sidebar).
    assert "async function renderFolderTree" in js
    assert "/diff`" in js
    assert "/api/workspace/tree?" in js
    # polling must stop on terminal states to avoid runaway intervals
    assert "stopLiveProgressPolling();" in js

def test_m16_panels_have_styles():
    css = STYLES.read_text(encoding="utf-8")
    assert ".progress-box" in css
    assert ".diff-row" in css
    assert ".file-tree-shell" in css
    assert ".file-tree-row" in css


def test_tier_badge_present_in_ui():
    html = (ROOT / "shared-ui" / "index.html").read_text(encoding="utf-8")
    assert "data-tier-badge" in html


def test_tier_badge_styled():
    css = STYLES.read_text(encoding="utf-8")
    assert ".tier-badge" in css
    assert ".tier-badge.is-manual" in css


def test_tier_preview_and_override_wired():
    js = APP_JS.read_text(encoding="utf-8")
    assert "/api/context/classify" in js
    assert "scheduleTierPreview" in js
    assert "consumeTierOverride" in js
    assert "runBody.context_tier" in js
