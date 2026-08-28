from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def _yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_4c8g_profile_relaxes_resources_without_adding_workers():
    services = _yaml("compose.4c8g.yml")["services"]
    app = services["app"]
    nginx = services["nginx"]

    assert app["environment"]["UVICORN_WORKERS"] == "1"
    assert app["cpus"] == 3.2
    assert app["mem_limit"] == "6g"
    assert app["environment"]["HTTP_MAX_CONNECTIONS"] == "256"
    assert app["environment"]["GENERATED_IMAGE_DOWNLOAD_CONCURRENCY"] == "64"
    assert nginx["cpus"] == 0.6
    assert nginx["mem_limit"] == "512m"


def test_cloudflare_profile_mounts_origin_certificate_and_https_proxy():
    nginx = _yaml("compose.cloudflare.yml")["services"]["nginx"]
    config = (ROOT / "deploy" / "nginx.cloudflare.conf").read_text(encoding="utf-8")

    assert "443:443" in nginx["ports"]
    assert any("origin.pem:/etc/nginx/certs/origin.pem:ro" in item for item in nginx["volumes"])
    assert any("origin.key:/etc/nginx/certs/origin.key:ro" in item for item in nginx["volumes"])
    assert "listen 443 ssl;" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert "proxy_buffering off;" in config


def test_production_image_contains_migration_and_backup_tools():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile


def test_runbook_explains_safe_password_to_key_migration():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    assert "已经使用密码登录" in runbook
    assert "保留原 root 窗口" in runbook
    assert "ssh-keygen -t ed25519" in runbook
    assert "PreferredAuthentications=publickey" in runbook
    assert "PermitRootLogin no" in runbook
    assert "PasswordAuthentication yes" in runbook
    assert "Recovery Console" in runbook


def test_runbook_explains_firewall_layers_and_port_purposes():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    assert "DigitalOcean Cloud Firewall（云防火墙）" in runbook
    assert "Inbound（入站）" in runbook
    assert "Outbound（出站）" in runbook
    assert "TCP 22" in runbook
    assert "TCP 80" in runbook
    assert "TCP 443" in runbook
    assert "不要开放这些端口" in runbook
