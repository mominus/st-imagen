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
    production_nginx = _yaml("compose.prod.yml")["services"]["nginx"]
    nginx = _yaml("compose.cloudflare.yml")["services"]["nginx"]
    config = (ROOT / "deploy" / "nginx.cloudflare.conf").read_text(encoding="utf-8")

    assert "443:443" in nginx["ports"]
    assert any("origin.pem:/etc/nginx/certs/origin.pem:ro" in item for item in nginx["volumes"])
    assert any("origin.key:/etc/nginx/certs/origin.key:ro" in item for item in nginx["volumes"])
    assert "listen 443 ssl;" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert "proxy_buffering off;" in config
    assert production_nginx["cap_drop"] == ["ALL"]
    assert set(production_nginx["cap_add"]) == {
        "CHOWN",
        "NET_BIND_SERVICE",
        "SETGID",
        "SETUID",
    }
    assert production_nginx["security_opt"] == ["no-new-privileges:true"]


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


def test_runbook_prevents_nested_clone_and_repairs_data_permissions():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    assert "git clone <你的仓库 SSH/HTTPS 地址> /opt/st-imagen" in runbook
    assert "/opt/st-imagen/st-imagen/.git" in runbook
    assert "fatal: not a git repository" in runbook
    assert "find /opt/st-imagen -maxdepth 3 -type d -name .git -print" in runbook
    assert runbook.index("chmod 750 data") < runbook.index("sudo chown -R 10001:10001 data")
    assert "sudo chmod 750 data" in runbook
    assert "Operation not permitted" in runbook


def test_runbook_secures_and_repairs_cloudflare_certificate_permissions():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )
    cert_readme = (ROOT / "deploy" / "certs" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key" in runbook
    assert "sudo chmod 644 deploy/certs/origin.pem" in runbook
    assert "sudo chmod 600 deploy/certs/origin.key" in runbook
    assert "cannot load certificate" in runbook
    assert "Permission denied" in runbook
    assert "docker compose $COMPOSE_FILES run --rm --no-deps nginx nginx -t" in runbook
    assert "root:root" in cert_readme
    assert "0644" in cert_readme
    assert "0600" in cert_readme


def test_runbook_repairs_nginx_temp_directory_capability_failure():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    assert 'chown("/var/cache/nginx/client_temp", 101) failed' in runbook
    assert "CHOWN" in runbook
    assert "SETGID" in runbook
    assert "SETUID" in runbook
    assert "chmod 777" in runbook
    assert "docker compose $COMPOSE_FILES up -d --force-recreate nginx" in runbook
