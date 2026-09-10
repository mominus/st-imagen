from pathlib import Path
import re


STATIC_ROOT = Path(__file__).parents[1] / "app" / "static"
ADMIN_HTML = STATIC_ROOT / "admin.html"
EXPECTED_ADMIN_SCRIPTS = [
    "/static/admin/core.js",
    "/static/admin/overview.js",
    "/static/admin/resources.js",
    "/static/admin/preview.js",
    "/static/admin/settings.js",
    "/static/admin/dialogs.js",
    "/static/admin/bootstrap.js",
]


def test_admin_feature_scripts_exist_and_load_in_dependency_order():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    script_sources = re.findall(r'<script src="([^"]+)"></script>', html)
    admin_sources = [source for source in script_sources if source.startswith("/static/admin/")]

    assert admin_sources == EXPECTED_ADMIN_SCRIPTS
    assert "/static/admin.js" not in script_sources
    for source in admin_sources:
        assert (STATIC_ROOT / source.removeprefix("/static/")).is_file()


def test_admin_uses_public_brand_logo_and_name():
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert html.count('src="/static/assets/logo.png"') == 2
    assert "后台 · 画点啥" in html
    assert "画点啥" in html
    # favicon links should be present in the admin head
    assert 'href="/static/assets/favicon.ico"' in html
    assert 'href="/static/assets/logo.png"' in html


def test_admin_has_specified_invites_dated_log_cleanup_and_branded_confirm():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    scripts = "\n".join(
        (STATIC_ROOT / "admin" / name).read_text(encoding="utf-8")
        for name in ("core.js", "resources.js", "settings.js", "dialogs.js")
    )

    assert 'id="i_generation_mode"' in html
    assert 'id="i_specified_codes"' in html
    assert 'id="deleteLogsBeforeInput"' in html
    assert 'id="deleteLogsBeforeInput" type="date" value="2026-01-01"' in html
    assert 'id="cleanupLogsBeforeInput"' not in html
    assert 'id="confirmModal"' in html
    assert "confirmAction(" in scripts
    assert "confirm(" not in scripts
    assert 'badgeHtml("exhausted", "warning")' in scripts


def test_logs_use_server_side_filters_and_fixed_size_pagination():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    resources = (STATIC_ROOT / "admin" / "resources.js").read_text(encoding="utf-8")

    assert 'id="logsPrevPage"' in html
    assert 'id="logsNextPage"' in html
    assert 'id="logsPageSummary"' in html
    assert "pageSize: 100" in (STATIC_ROOT / "admin" / "core.js").read_text(encoding="utf-8")
    assert 'params.set("query"' in resources
    assert 'params.set("status"' in resources
    assert 'params.set("mode"' in resources
    assert 'params.set("failure_category"' in resources
    assert "filteredLogs" not in resources
    assert "limit=200" not in resources


def test_users_default_to_generation_count_sort_and_announcements_have_management_ui():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    resources = (STATIC_ROOT / "admin" / "resources.js").read_text(encoding="utf-8")
    settings = (STATIC_ROOT / "admin" / "settings.js").read_text(encoding="utf-8")

    assert "Number(b.total_requests || 0) - Number(a.total_requests || 0)" in resources
    assert 'id="announcementTitleInput"' in html
    assert 'id="adminAnnouncementList"' in html
    assert 'api("/api/admin/announcements")' in settings


def test_public_announcements_require_explicit_read_confirmation_without_polling():
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="announcementBadge"' in html
    assert 'id="announcementReadBtn"' in html
    assert "function confirmAnnouncementsRead()" in app_js
    assert "localStorage.setItem(ANNOUNCEMENT_READ_KEY" in app_js
    assert "setInterval(loadAnnouncements" not in app_js
    close_body = app_js.split("function closeAnnouncements()", 1)[1].split("}\n", 1)[0]
    assert "localStorage.setItem" not in close_body
