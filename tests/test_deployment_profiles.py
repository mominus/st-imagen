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


def test_runbook_uses_ssh_key_as_the_only_normal_deployment_path():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    normal_path = runbook[: runbook.index("## 12. 集中故障排查")]
    assert "创建 DigitalOcean Droplet（使用 SSH key）" in normal_path
    assert "Authentication Method" in normal_path
    assert "不选择 Password" in normal_path
    assert "PreferredAuthentications=publickey" in normal_path
    assert "00-st-imagen-hardening.conf" in normal_path
    assert "PermitRootLogin no" in normal_path
    assert "PasswordAuthentication no" in normal_path
    assert "不要在 `root@ubuntu...#` 后运行 ssh" in normal_path
    assert "IdentitiesOnly=yes" in normal_path
    assert "绝不能为了测试把私钥上传" in normal_path
    assert normal_path.index("独立窗口验证 deploy key") < normal_path.index(
        "最后才关闭 root/password SSH"
    )


def test_runbook_moves_password_recovery_out_of_the_normal_path():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    troubleshooting = runbook[runbook.index("## 12. 集中故障排查") :]
    assert "Access → Reset Root Password" in troubleshooting
    assert "sshd -T -C user=root" in troubleshooting
    assert "00-emergency-recovery.conf" in troubleshooting
    assert "PermitRootLogin yes" in troubleshooting
    assert "passwd -S root" in troubleshooting
    assert "Droplet 视为已失陷" in troubleshooting


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
    assert "sudo test -d data/uploads/generated" in runbook
    assert "sudo ls -ldn data data/uploads data/uploads/generated" in runbook
    assert "sudo setpriv --reuid=10001 --regid=10001 --clear-groups" in runbook
    assert "unknown user #10001" in runbook
    assert "IMAGE=$(sudo find data/uploads/generated" in runbook
    assert "普通 deploy 用户" in runbook


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
    assert "sudo tee deploy/certs/origin.pem >/dev/null" in runbook
    assert "sudo tee deploy/certs/origin.key >/dev/null" in runbook
    assert "editing files in a writable directory is not permitted" in runbook
    assert "sudoedit deploy/certs/origin.pem" not in runbook
    assert "sudoedit deploy/certs/origin.key" not in runbook
    assert "sudo test -s deploy/certs/origin.pem" in runbook
    assert "sudo test -s deploy/certs/origin.key" in runbook
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


def test_runbook_diagnoses_non_json_upstream_responses_without_printing_keys():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    assert "登录正常但生图 502" in runbook
    assert "https://upstream.example.com" in runbook
    assert "不是本站域名" in runbook
    assert "--force-recreate app" in runbook
    assert 'os.environ.get("ST_BASE_URL", "")' in runbook
    assert "不会输出 API key" in runbook
    assert "Content-Type" in runbook


def test_runbook_repairs_public_upload_permissions_without_exposing_database():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    assert 'open() "/srv/uploads/generated/gen-....jpg" failed' in runbook
    assert "sudo chmod 750 data" in runbook
    assert "sudo find data/uploads -type d -exec chmod 755 {} +" in runbook
    assert "sudo find data/uploads -type f -exec chmod 644 {} +" in runbook
    assert "exec --user 101 nginx" in runbook
    assert "不要对整个 `data` 执行 `chmod -R 755`" in runbook


def test_runbook_keeps_known_failures_in_one_troubleshooting_section():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )

    troubleshooting_at = runbook.index("## 12. 集中故障排查")
    for marker in (
        "fatal: not a git repository",
        "Operation not permitted",
        "登录正常但生图 502",
        "cannot load certificate",
        'chown(\"/var/cache/nginx/client_temp\", 101) failed',
    ):
        assert runbook.index(marker) > troubleshooting_at
    assert "正常部署只按 1～11 节顺序执行" in runbook


def test_runbook_diagnoses_deploy_public_key_from_the_local_machine():
    runbook = (ROOT / "docs" / "deploy-digitalocean-cloudflare.md").read_text(
        encoding="utf-8"
    )
    troubleshooting = runbook[runbook.index("## 12. 集中故障排查") :]

    assert "deploy 报 `Permission denied (publickey)`" in troubleshooting
    assert "VPS 上错误地连接" in troubleshooting
    assert "namei -l /home/deploy/.ssh/authorized_keys" in troubleshooting
    assert "ssh -vvv -o IdentitiesOnly=yes" in troubleshooting
    assert "不要把 `id_ed25519` 私钥复制到服务器" in troubleshooting
