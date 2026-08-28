# DigitalOcean 4C8G + Cloudflare 完整部署手册

本文适用于以下迁移路径：

1. 先在 DigitalOcean 的 Ubuntu VPS 上临时以 **4 vCPU / 8 GiB** 运行；
2. 域名 DNS 和公网 TLS 由 Cloudflare 管理；
3. 到期后迁移到阿里云 **2 vCPU / 2 GiB**，恢复仓库默认资源限制。

> 本项目必须保持 `UVICORN_WORKERS=1`。账号槽位、全局准入、限流和熔断状态均在进程内；4C8G
> 通过提高容器 CPU/内存、连接池和图片落盘并发来放宽资源，而不是增加 worker。

## 0. 部署拓扑与文件

```text
浏览器 → Cloudflare（DNS/CDN/TLS）→ VPS:443 nginx → app:8001
                                      ├─ /static 直接读取仓库静态文件
                                      └─ /uploads 直接读取 data/uploads
```

会组合三个 Compose 文件：

- `compose.prod.yml`：长期 2C2G 基线；
- `compose.cloudflare.yml`：Cloudflare Origin CA TLS；
- `compose.4c8g.yml`：DigitalOcean 临时资源覆盖层。

服务器私有文件：

- `.env`：密钥和运行参数，不提交 Git；
- `deploy/certs/origin.pem`、`deploy/certs/origin.key`：Cloudflare Origin CA 证书和私钥；
- `data/`：SQLite、参考图、生成图和备份，是迁移时必须复制的持久化目录。

## 1. 创建 DigitalOcean Droplet（使用 SSH key）

后续新 VPS 统一使用 SSH key，不再走 root 密码部署路径。

### 1.1 在本机准备密钥

如果 Termius 已有专用 Ed25519 私钥，可直接使用；否则在自己的电脑生成：

```bash
ssh-keygen -t ed25519 -a 64 -C "digitalocean-st-imagen"
```

私钥只能保存在自己的设备；向 DigitalOcean 添加的是 `.pub` 公钥。在创建 Droplet 的
**Authentication Method** 选择 **SSH Key**，勾选这把公钥，不选择 Password。

### 1.2 Droplet 参数

- Ubuntu 24.04 LTS x64；
- 临时规格 4 vCPU / 8 GiB，磁盘至少 50 GiB；
- 绑定 Reserved IP；
- 选择主要用户与上游延迟较低的区域；
- 创建时绑定刚才确认过的 SSH key。

记录：

```bash
export VPS_IP="203.0.113.10"
export DOMAIN="img.example.com"
```

### 1.3 Cloud Firewall

DigitalOcean Cloud Firewall（云防火墙）在流量到达 Ubuntu 前过滤；UFW 是系统防火墙，
应用登录则是第三层，三者不能互相替代。

- Inbound（入站）TCP 22：只允许你当前公网 IP `/32`；IP 变化先更新规则；
- Inbound TCP 80：联调期允许全部，稳定后只允许 Cloudflare 官方网段；
- Inbound TCP 443：联调期允许全部，稳定后只允许 Cloudflare 官方网段；
- Outbound（出站）：保留 All traffic，供 apt、GitHub、Docker、DNS、NTP 和上游 API 使用。

不要开放这些端口：8001、5432、3306、2375、2376。开始前确认 DigitalOcean 的 Recovery
Console/Recovery 页面可用；22 端口不要填 Cloudflare IP。

## 2. 首次 SSH key 登录与系统加固

### 2.1 首次登录并创建运维用户

在本机连接：

```bash
ssh -o PreferredAuthentications=publickey root@"$VPS_IP"
```

若失败，先修正 DigitalOcean 绑定的公钥或 Termius 私钥选择，不要临时改用弱密码。登录后
保持 root 窗口开启：

```bash
apt update
apt full-upgrade -y
adduser deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
```

### 2.2 独立窗口验证 deploy key 和 sudo

另开 Termius/终端窗口：

```bash
ssh -o PreferredAuthentications=publickey deploy@"$VPS_IP"
sudo -v
whoami
sudo whoami
```

必须依次确认 deploy 公钥登录成功、`sudo whoami` 输出 `root`、Recovery Console 可用；
原 root 窗口和新 deploy 窗口都保持打开。

### 2.3 最后才关闭 root/password SSH

在 deploy 会话执行：

```bash
sudoedit /etc/ssh/sshd_config.d/00-st-imagen-hardening.conf
```

写入：

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

验证最终生效值后才 reload：

```bash
sudo sshd -t
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
sudo systemctl reload ssh
```

OpenSSH 多数认证项是先读到的值生效，`00-st-imagen-hardening.conf` 必须排在 Ubuntu 的
`50-cloud-init.conf` 前。再开第三个窗口测试 `ssh deploy@$VPS_IP`；成功后才退出 root。

### 2.4 基础工具与时间

```bash
sudo apt install -y unattended-upgrades ca-certificates curl git rsync
sudo dpkg-reconfigure -plow unattended-upgrades
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true
timedatectl status
```

## 3. 按 Docker 官方仓库安装 Engine 与 Compose

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy
sudo systemctl enable --now docker
```

退出 SSH 并重新登录，让 `docker` 组生效，然后验证：

```bash
docker version
docker compose version
docker run --rm hello-world
```

> `docker` 组等同于主机 root 权限，只给实际维护者加入该组。

## 4. 拉取代码并准备持久化目录

以下全部在 deploy 会话执行。目标路径本身就是仓库根目录，不会再套一层同名目录：

```bash
cd /opt
sudo install -d -m 755 -o deploy -g deploy /opt/st-imagen
git clone <你的仓库 SSH/HTTPS 地址> /opt/st-imagen
cd /opt/st-imagen
test -d .git
git status --short --branch
git checkout main
git pull --ff-only
```

创建目录时一次使用最终正确权限：`data` 保护数据库，只有公开 uploads 允许 nginx 读取。

```bash
mkdir -p data/uploads/generated data/backups deploy/certs
chmod 750 data
chmod 755 data/uploads data/uploads/generated
sudo chown -R 10001:10001 data
```

最终检查：

```bash
pwd
test -d .git
test -f compose.prod.yml
test -d data/uploads/generated
git rev-parse --short HEAD
ls -ldn data data/uploads data/uploads/generated
```

`pwd` 必须是 `/opt/st-imagen`，而不是 `/opt/st-imagen/st-imagen`。

## 5. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

先构建镜像，以便使用镜像内的 `cryptography` 生成 Fernet key：

```bash
docker compose -f compose.prod.yml -f compose.4c8g.yml build app
FERNET_KEY=$(docker compose -f compose.prod.yml -f compose.4c8g.yml run --rm --no-deps app \
  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
JWT_KEY=$(openssl rand -base64 48 | tr -d '\n')
printf 'Fernet: %s\nJWT: %s\n' "$FERNET_KEY" "$JWT_KEY"
```

用 `nano .env` 或 `sudoedit .env` 至少修改：

```dotenv
DEBUG=false
UVICORN_WORKERS=1
ENCRYPTION_KEY=<上面的 Fernet 值>
JWT_SECRET_KEY=<上面的 JWT 值>
ADMIN_USERNAME=<非默认管理员名>
ADMIN_PASSWORD=<密码管理器生成的强密码>
ADMIN_PATH=<随机且不公开的后台路径>
ST_BASE_URL=<真实 ST API 根地址>
PUBLIC_BASE_URL=https://img.example.com
USER_SESSION_SECURE=true
USER_SESSION_SAMESITE=lax
CORS_ORIGINS=https://img.example.com
CORS_ALLOW_CREDENTIALS=true
ST_TRUST_ENV=false
```

注意：

- 不要修改 Fernet key；否则数据库内已有 API key 无法解密；
- `ADMIN_PATH` 只是降低入口曝光，不替代管理员认证；
- 同源部署可把 `CORS_ORIGINS` 留空；若填写，必须是精确 HTTPS Origin，末尾不加 `/`；
- 4C8G 覆盖层已经把连接池和下载并发放宽，不要为了利用 4 核把 worker 改成 4；
- `GENERATION_GLOBAL_MAX_CONCURRENT` 和各账号 `max_inflight` 仍应按上游承载能力压测后设置，CPU 变多不代表上游额度变多。

`ST_BASE_URL` 必须是上游 API 根地址，不是本站域名、网页首页、示例占位符或具体
`/inference/...` 路径。保存配置后继续第 6 节；如果后台测试返回 502，再查第 12.4 节，
不要在正常部署流程中提前启动半配置状态的容器。

## 6. 在 Cloudflare 创建 DNS 与 Origin CA 证书

### 6.1 DNS

Cloudflare → **DNS → Records**：

- Type：`A`；
- Name：例如 `img`；
- IPv4：DigitalOcean Reserved IP/Droplet IP；
- Proxy status：**Proxied（橙云）**；
- TTL：Auto。

如没有配置可用 IPv6，不要创建 AAAA 记录。等待 DNS 后检查：

```bash
dig +short "$DOMAIN" @1.1.1.1
```

开启橙云时结果通常是 Cloudflare Anycast IP，而不是源站 IP。

### 6.2 Origin CA

Cloudflare → **SSL/TLS → Origin Server → Create certificate**：

- 私钥类型：ECC 或 RSA 均可；
- Hostnames：`img.example.com`（需要时再加 `*.example.com`）；
- 有效期：按你的密钥轮换政策选择。

把 Origin Certificate 和 Private Key 分别写入服务器：

```bash
cd /opt/st-imagen
sudo install -o root -g root -m 644 /dev/null deploy/certs/origin.pem
sudo install -o root -g root -m 600 /dev/null deploy/certs/origin.key
sudoedit deploy/certs/origin.pem
sudoedit deploy/certs/origin.key
sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key
sudo chmod 644 deploy/certs/origin.pem
sudo chmod 600 deploy/certs/origin.key
```

粘贴时保留完整的 `BEGIN/END` 行。证书本身不含私钥，可以是 `644`；私钥必须保持
`600`。这里特意让两者归 `root:root`：生产 Compose 删除了 nginx 的文件权限绕过
能力，若文件归普通 `deploy` 用户且为 `600`，容器内 nginx 主进程即使是 UID 0 也可能
收到 `Permission denied`。检查证书：

```bash
sudo openssl x509 -in deploy/certs/origin.pem -noout -subject -issuer -dates
sudo openssl pkey -in deploy/certs/origin.key -check -noout
```

Cloudflare → **SSL/TLS → Overview** 设置为 **Full (strict)**，不要使用 Flexible。随后可开启：

- Edge Certificates → Always Use HTTPS；
- Edge Certificates → Automatic HTTPS Rewrites；
- HSTS 仅在确认域名及所有需要的子域都能长期 HTTPS 后开启，避免误锁站点。

建议添加 Cache Rules：

1. `/api/*`、随机后台路径、`/health*`：Bypass cache；
2. `/uploads/*`：尊重源站 `Cache-Control`；
3. 不要对整个站点使用 “Cache Everything”，否则登录态/API 可能被错误缓存。

## 7. 校验并启动临时 4C8G 部署

```bash
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml -f compose.4c8g.yml'
docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES config > /tmp/st-imagen.compose.yml
sudo stat -c '%U:%G %a %n' deploy/certs/origin.pem deploy/certs/origin.key
docker compose $COMPOSE_FILES run --rm --no-deps nginx nginx -t
docker compose $COMPOSE_FILES up -d --build
docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=200 app nginx
```

证书预期 `root:root 644`，私钥预期 `root:root 600`。新数据库由应用初始化；已有数据库再执行：

```bash
docker compose $COMPOSE_FILES run --rm app alembic upgrade head
```

不要在此处再次递归修改整个 `data`；第 4 节已经同时设置 app 所有权和 nginx uploads 读取权限。

## 8. 分层验收

### 8.1 源站本机

Origin CA 不被普通浏览器直接信任，因此本机测试指定 Host 并忽略 CA 信任链：

```bash
curl -k --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/health/live"
curl -k --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/health/ready"
```

预期分别返回 `{"status":"ok"}` 和 ready 状态。

### 8.2 Cloudflare 公网

```bash
curl -I "https://$DOMAIN/"
curl -fsS "https://$DOMAIN/health/live"
curl -fsS "https://$DOMAIN/health/ready"
```

浏览器检查：

- 首页、登录、退出、后台随机路径；
- 上传参考图；
- 发起一次生成并确认 SSE 进度不中断；
- 生成图片 URL 是 `https://$DOMAIN/uploads/...`；
- Cookie 具有 `Secure`、`HttpOnly`、期望的 `SameSite`；
- Cloudflare 没有缓存 `/api/auth/status` 等动态接口。

### 8.3 资源覆盖是否生效

```bash
docker inspect st-imagen-app --format 'CPU={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} Swap={{.HostConfig.MemorySwap}}'
docker stats --no-stream
docker compose $COMPOSE_FILES exec app sh -c 'echo workers=$UVICORN_WORKERS http=$HTTP_MAX_CONNECTIONS downloads=$GENERATED_IMAGE_DOWNLOAD_CONCURRENCY db=$DB_POOL_SIZE'
```

4C8G 预期：app 约 3.2 CPU、6 GiB；nginx 约 0.6 CPU、512 MiB；宿主机仍保留资源给 Docker、文件缓存和 SSH。

## 9. 日常更新、监控与备份

更新前先备份：

```bash
cd /opt/st-imagen
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose $COMPOSE_FILES exec app python scripts/backup_data.py --include-uploads
tar --acls --xattrs -C /opt -czf "/home/deploy/st-imagen-$STAMP.tgz" st-imagen/data st-imagen/.env st-imagen/deploy/certs
chmod 600 "/home/deploy/st-imagen-$STAMP.tgz"
```

更新：

```bash
git fetch --all --prune
git checkout main
git pull --ff-only
docker compose $COMPOSE_FILES build --pull
docker compose $COMPOSE_FILES run --rm app alembic upgrade head
docker compose $COMPOSE_FILES up -d
docker compose $COMPOSE_FILES ps
curl -fsS "https://$DOMAIN/health/ready"
```

常用排障：

```bash
docker compose $COMPOSE_FILES logs -f --tail=200 app nginx
docker stats
df -h /opt/st-imagen/data
sudo journalctl -u docker --since '1 hour ago'
```

归档包含 `.env` 和 TLS 私钥，必须在上传对象存储前使用 `age`、GPG 或等效工具加密，并在完成恢复/传输后删除服务器上的临时明文归档。建议至少每天把 `data/` 的加密备份同步到独立对象存储；只有同一 VPS 上的备份不具备灾难恢复能力。定期做恢复演练，而不只是确认备份文件存在。

## 10. 到期后迁移到阿里云 2C2G

### 10.1 准备新机

阿里云新机同样在创建时绑定 SSH key，并按第 2 节建立 deploy 公钥登录，不使用 root 密码部署。
安全组开放策略与 DigitalOcean 相同：

- SSH 22 仅允许运维 IP；
- 80/443 只允许 Cloudflare 官方 IP 段（初次联调可短暂放宽）；
- 安装同版本 Docker Engine/Compose；
- 克隆相同 Git commit；
- 创建 `/opt/st-imagen`。

### 10.2 停写与最终备份

选择维护窗口，在 DigitalOcean 旧机执行：

```bash
cd /opt/st-imagen
docker compose $COMPOSE_FILES stop nginx
docker compose $COMPOSE_FILES exec app python scripts/backup_data.py --include-uploads || true
docker compose $COMPOSE_FILES stop app
sudo tar --acls --xattrs -czf /home/deploy/st-imagen-final.tgz data .env deploy/certs
sudo chmod 600 /home/deploy/st-imagen-final.tgz
sha256sum /home/deploy/st-imagen-final.tgz > /home/deploy/st-imagen-final.tgz.sha256
```

此时旧站停止接收写入，避免复制 SQLite 时产生分叉。

复制到阿里云：

```bash
rsync -avP /home/deploy/st-imagen-final.tgz* deploy@<ALIYUN_IP>:/home/deploy/
```

### 10.3 在 2C2G 恢复

阿里云新机：

```bash
cd /opt/st-imagen
sha256sum -c /home/deploy/st-imagen-final.tgz.sha256
sudo tar --acls --xattrs -xzf /home/deploy/st-imagen-final.tgz -C /opt/st-imagen
sudo chown -R 10001:10001 data
sudo chmod 750 data
sudo find data/uploads -type d -exec chmod 755 {} +
sudo find data/uploads -type f -exec chmod 644 {} +
sudo chown deploy:deploy .env
sudo chmod 600 .env
sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key
sudo chmod 644 deploy/certs/origin.pem
sudo chmod 600 deploy/certs/origin.key
```

**2C2G 启动时不要再包含 `compose.4c8g.yml`：**

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES up -d --build
docker compose $COMPOSE_FILES run --rm app alembic upgrade head
docker compose $COMPOSE_FILES ps
```

这样会自动恢复 `compose.prod.yml` 的长期限制：app 1.6 CPU / 1400 MiB，nginx 0.4 CPU / 300 MiB，且 worker 仍为 1。

### 10.4 切换 Cloudflare DNS

1. 把 Cloudflare A 记录改为阿里云公网 IP；
2. 保持橙云和 Full (strict)；
3. 连续验证首页、后台、登录、上传、SSE 和图片；
4. 观察阿里云日志与资源至少一个业务高峰；
5. 确认新备份链可用后再销毁 DigitalOcean Droplet。

Cloudflare 开启代理时 DNS TTL 对终端用户影响较小，但边缘到源站切换仍可能短暂复用旧连接；建议旧机在切换后保留一段观察时间，不要立即销毁。

## 11. 回滚

应用更新失败：

```bash
git checkout <上一个已验证 commit>
docker compose $COMPOSE_FILES up -d --build
```

迁移失败：把 Cloudflare A 记录切回 DigitalOcean IP，并用原来的三个 Compose 文件重新启动旧机。不要让新旧两台同时接受写入后再合并 SQLite。

## 12. 集中故障排查

正常部署只按 1～11 节顺序执行。本节只在出现对应错误时使用，不要把修复命令混入首次部署。

### 12.1 SSH 握手成功但 root 密码失败

新建 VPS 应使用第 1 节 SSH key 流程。旧密码机出现 `Authentication failed (password)` 时，
22 端口和 sshd 已经可达；通过 DigitalOcean **Access → Reset Root Password** 或 Recovery
Console/ISO 恢复。恢复后检查：

```bash
passwd -S root
chage -l root
sshd -T -C user=root,host="$(hostname)",addr="你的当前公网IP" \
  | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
```

只有 root 密码时不要跳过 2.3 和 2.4 后又执行禁用 root/password 的命令。紧急临时文件可用
`00-emergency-recovery.conf` 设置 `PermitRootLogin yes`、`PasswordAuthentication yes`，但
验证 deploy key 后必须删除。若存在陌生登录/公钥或密码自行变化，把 Droplet 视为已失陷并
重建、轮换所有密钥。不要长期退回密码部署。

### 12.2 仓库目录错误

`fatal: not a git repository` 时：

```bash
find /opt/st-imagen -maxdepth 3 -type d -name .git -print
```

若看到 `/opt/st-imagen/st-imagen/.git`，说明旧命令克隆了嵌套目录；全新且无数据时删除后按
第 4 节重新克隆。已有 `.env`、证书或数据时先备份，不能直接 `rm -rf`。

### 12.3 `data` 权限或图片 `Permission denied`

`chmod: ... Operation not permitted` 是宿主机 deploy 已不再拥有 UID 10001 文件。
存储计数增加但 nginx 日志出现
`open() "/srv/uploads/generated/gen-....jpg" failed (13: Permission denied)` 时执行：

```bash
cd /opt/st-imagen
sudo chown -R 10001:10001 data
sudo chmod 750 data
sudo find data/uploads -type d -exec chmod 755 {} +
sudo find data/uploads -type f -exec chmod 644 {} +
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml -f compose.4c8g.yml'
IMAGE=$(find data/uploads/generated -maxdepth 1 -type f -printf '%f\n' | head -n 1)
docker compose $COMPOSE_FILES exec --user 101 nginx test -r "/srv/uploads/generated/$IMAGE"
```

不要对整个 `data` 执行 `chmod -R 755`；数据库仍需保护。权限立即生效，无需重启容器。

### 12.4 登录正常但生图 502 非 JSON

确认 `.env` 的 `ST_BASE_URL` 是真实上游 API 根地址，不是本站域名、
`https://upstream.example.com` 或具体 inference 路径。修改后必须 `--force-recreate app`。
以下检查不会输出 API key：

```bash
docker compose $COMPOSE_FILES exec app python - <<'UPSTREAMPY'
import os
from urllib.parse import urlsplit
value = os.environ.get("ST_BASE_URL", "")
parsed = urlsplit(value)
print(value, parsed.scheme, parsed.hostname, parsed.path or "/")
UPSTREAMPY
```

错误中的 HTTP 状态、Content-Type 和大小可区分 HTML 拦截页与空响应，不要公开响应正文。

### 12.5 nginx 证书不可读

`cannot load certificate ... Permission denied`：

```bash
sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key
sudo chmod 644 deploy/certs/origin.pem
sudo chmod 600 deploy/certs/origin.key
docker compose $COMPOSE_FILES run --rm --no-deps nginx nginx -t
```

### 12.6 nginx 临时目录不能 chown

`chown("/var/cache/nginx/client_temp", 101) failed` 表示旧版 Compose 缺 capability。更新代码，
确认 `compose.prod.yml` 的 `cap_add` 包含 `CHOWN`、`NET_BIND_SERVICE`、`SETGID`、`SETUID`，然后：

```bash
git pull --ff-only
docker compose $COMPOSE_FILES up -d --force-recreate nginx
```

不要使用 `chmod 777` 或 `privileged: true`。

## 官方参考

- DigitalOcean Droplet 创建与基础操作：<https://docs.digitalocean.com/products/droplets/how-to/create/>
- DigitalOcean Cloud Firewalls：<https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/>
- Docker Engine on Ubuntu：<https://docs.docker.com/engine/install/ubuntu/>
- Docker Linux post-install：<https://docs.docker.com/engine/install/linux-postinstall/>
- Cloudflare 代理状态：<https://developers.cloudflare.com/dns/proxy-status/>
- Cloudflare Full (strict)：<https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/>
- Cloudflare Origin CA：<https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
- Cloudflare IP ranges：<https://www.cloudflare.com/ips/>
