from pathlib import Path

from app.routers.generate import GenerateResponse


ROOT = Path(__file__).parents[1]
TEXT_SUFFIXES = {".html", ".js", ".css", ".py", ".md", ".yml", ".yaml", ".toml"}


def test_repository_does_not_publish_provider_brand_tokens():
    provider = "sta" + "ckai"
    forbidden = (provider, provider.replace("ai", "-ai"))
    matches = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(token in text or token in path.name.lower() for token in forbidden):
            matches.append(str(path.relative_to(ROOT)))
    assert matches == []


def test_public_generation_response_excludes_backend_account_identity():
    payload = GenerateResponse(
        success=True,
        images=["/uploads/generated/result.webp"],
        response_time_ms=123,
    ).model_dump()

    assert "account_id" not in payload
    assert "account_name" not in payload
    assert "org_id" not in payload
    assert "flow_id" not in payload


def test_browser_stream_uses_sanitized_progress_events_only():
    router_source = (ROOT / "app" / "routers" / "generate.py").read_text(encoding="utf-8")
    frontend_source = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert '"type": "upstream"' not in router_source
    assert 'evt.type === "upstream"' not in frontend_source
    assert '"type": "progress"' in router_source
    assert 'evt.type === "progress"' in frontend_source


def test_public_brand_uses_the_supplied_logo_and_name():
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    logo = ROOT / "app" / "static" / "assets" / "see-you-logo.png"

    assert logo.is_file()
    assert logo.stat().st_size > 0
    assert logo.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert html.count('src="/static/assets/see-you-logo.png"') == 2
    assert "<title>see you · imagen — 让想象，显影</title>" in html
    assert "see you · imagen" in html
