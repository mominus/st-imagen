from datetime import datetime, timedelta, timezone

import pytest

from app.services.dashboard_analytics import (
    aggregate_dashboard_rows,
    normalize_failure_category,
    normalize_gpt_size,
    normalize_model_key,
    normalize_nano_resolution,
    period_window,
)


UTC = timezone.utc


def log_row(at, *, status="success", mode="text2img", model="Nano Banana Pro", resolution="2K", aspect_ratio="1:1", response_time_ms=100, error_message=None):
    return {
        "timestamp": at,
        "status": status,
        "response_time_ms": response_time_ms,
        "error_message": error_message,
        "mode": mode,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }


def test_period_window_uses_utc_and_validates_periods():
    now = datetime(2026, 8, 26, 12, 34, 56, tzinfo=UTC)
    start, end = period_window("24h", now=now)
    assert end == now
    assert start == datetime(2026, 8, 25, 12, 34, 56, tzinfo=UTC)
    with pytest.raises(ValueError):
        period_window("90d", now=now)


def test_empty_period_has_expected_hour_and_day_buckets():
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    assert len(aggregate_dashboard_rows([], "24h", now=now)["timeline"]) == 25
    assert len(aggregate_dashboard_rows([], "7d", now=now)["timeline"]) == 8
    assert len(aggregate_dashboard_rows([], "30d", now=now)["timeline"]) == 31


def test_timeline_includes_the_current_hour_bucket():
    now = datetime(2026, 8, 26, 12, 34, 56, tzinfo=UTC)
    data = aggregate_dashboard_rows([log_row(now - timedelta(hours=24)), log_row(now)], "24h", now=now)
    assert data["timeline"][-1]["start"] == "2026-08-26T12:00:00Z"
    assert data["timeline"][-1]["requests"] == 1
    assert sum(item["requests"] for item in data["timeline"]) == data["summary"]["requests"]


def test_failure_classification_covers_fixed_categories_in_priority_order():
    assert normalize_failure_category("当前账号并发已达上限") == "capacity"
    assert normalize_failure_category("生成成功，但保存图片失败：下载上游图片超时") == "storage"
    assert normalize_failure_category("账号 API key invalid") == "account_config"
    assert normalize_failure_category("参考图 URL 不可访问") == "reference_input"
    assert normalize_failure_category("上游服务超时") == "upstream"
    assert normalize_failure_category("无法判断的错误") == "other"


def test_model_and_spec_normalization():
    assert normalize_model_key("GPT Image 2") == "gpt_image_2"
    assert normalize_model_key("gemini-3-pro-image-preview") == "nano_banana_pro"
    assert normalize_model_key("gpt-image-1.5") == "other"
    assert normalize_nano_resolution("4k") == "4K"
    assert normalize_nano_resolution("8K") == "unknown"
    assert normalize_gpt_size("1024x1024") == "1k"
    assert normalize_gpt_size("1536x1024") == "1k"
    assert normalize_gpt_size("2048x2048") == "2k"
    assert normalize_gpt_size("3840x2160") == "4k"
    assert normalize_gpt_size("auto") == "auto"
    assert normalize_gpt_size("123x456") == "unknown"


def test_aggregate_keeps_img2img_out_of_model_cards_and_counts_all_rows():
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    rows = [
        log_row(now.replace(hour=11), model="Nano Banana Pro", resolution="1K"),
        log_row(now.replace(hour=10), model="Nano Banana Pro", resolution="4K", status="error", error_message="上游服务 503"),
        log_row(now.replace(hour=9), model="GPT Image 2", resolution="High", aspect_ratio="2048x2048"),
        log_row(now.replace(hour=8), model="GPT Image 2", resolution="Low", aspect_ratio="1536x1024", status="error", error_message="参考图输入无效"),
        log_row(now.replace(hour=7), model="gpt-image-1.5"),
        log_row(now.replace(hour=6), mode="img2img", model="Nano Banana Pro", resolution="2K"),
    ]
    data = aggregate_dashboard_rows(rows, "24h", now=now)
    assert data["summary"] == {"requests": 6, "success": 4, "failure": 2, "avg_response_ms": 100.0}
    models = {item["key"]: item for item in data["models"]}
    assert models["nano_banana_pro"]["requests"] == 2
    assert models["nano_banana_pro"]["failure"] == 1
    assert models["gpt_image_2"]["requests"] == 2
    assert models["gpt_image_2"]["failure"] == 1
    assert models["other"]["requests"] == 1
    assert sum(item["requests"] for item in models.values()) == 5
    nano_specs = {item["key"]: item for item in models["nano_banana_pro"]["specs"][0]["items"]}
    assert nano_specs["1k"]["requests"] == 1
    assert nano_specs["4k"]["failure"] == 1
    gpt_specs = {group["key"]: group for group in models["gpt_image_2"]["specs"]}
    gpt_sizes = {item["key"]: item for item in gpt_specs["size"]["items"]}
    assert gpt_sizes["2k"]["requests"] == 1
    assert gpt_sizes["1k"]["failure"] == 1


def test_aggregate_is_not_limited_to_recent_log_page_size():
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    rows = [log_row(now - timedelta(minutes=index % 60)) for index in range(250)]
    data = aggregate_dashboard_rows(rows, "24h", now=now)
    assert data["summary"]["requests"] == 250


def test_persisted_failure_category_takes_precedence_over_message_heuristics():
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    row = log_row(now, status="error", error_message="opaque provider failure")
    row["failure_category"] = "storage"
    data = aggregate_dashboard_rows([row], "24h", now=now)
    failures = {item["key"]: item["count"] for item in data["failures"]}
    assert failures["storage"] == 1
    assert failures["other"] == 0
