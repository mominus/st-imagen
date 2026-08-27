from app.services.st_client import extract_image_urls


def test_extract_image_urls_ignores_unrelated_urls_in_same_event():
    response = {
        "outputs": {
            "out-0": '{"image_url":"https://cdn.example/result.webp"}',
        },
        "status_url": "https://example.com/status/123",
    }

    assert extract_image_urls(response) == ["https://cdn.example/result.webp"]


def test_extract_image_urls_accepts_signed_image_extension_url():
    response = {
        "outputs": {
            "out-0": "https://cdn.example/generated/result.png?token=secret",
        }
    }

    assert extract_image_urls(response) == [
        "https://cdn.example/generated/result.png?token=secret"
    ]


def test_extract_image_urls_accepts_url_in_explicit_image_field():
    response = {
        "outputs": {
            "out-0": {
                "image_url": "https://cdn.example/object/opaque-identifier",
            }
        }
    }

    assert extract_image_urls(response) == [
        "https://cdn.example/object/opaque-identifier"
    ]
