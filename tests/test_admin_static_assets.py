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
