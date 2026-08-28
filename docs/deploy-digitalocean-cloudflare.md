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

## 1. 创建 DigitalOcean Droplet

在 DigitalOcean 控制台创建 Droplet：

- 系统：Ubuntu 24.04 LTS x64；
- 临时规格：4 vCPU / 8 GiB；
- 磁盘：至少 50 GiB，并预留不少于 `.env` 中 `GENERATED_IMAGE_MIN_FREE_BYTES` 的空间；
- 区域：选择主要用户和上游网络延迟较低的区域；
- 建议绑定 Reserved IP，避免重建 Droplet 后再次修改 DNS；
- 新建机器优先选择 SSH key；**如果已经选择密码登录，不必重建 VPS**，按第 2 节安全地迁移即可。

记录：

```bash
export VPS_IP="203.0.113.10"
export DOMAIN="img.example.com"
```

这里的 `export` 只是在**当前终端**给 IP 和域名起一个变量名，后续命令可以写 `$VPS_IP`，不会修改
DigitalOcean 或 Cloudflare。重新打开终端后要再次执行，或者直接把命令里的变量换成真实值。

### 1.1 先理解三层防护

部署中会遇到三种不同的“防护”，它们不能互相替代：

1. **DigitalOcean Cloud Firewall（云防火墙）**：在流量抵达 VPS 前过滤数据包，作用对象是 Droplet；本文的防火墙规则主要指它。
2. **VPS 系统防火墙（例如 UFW）**：在 Ubuntu 内过滤流量。Docker 发布端口会创建自己的转发规则，因此不能把 UFW 当作 Docker 端口的唯一保护。
3. **应用登录/权限**：管理员密码、普通用户会话和随机后台路径；它们只在请求已经到达应用后生效。

### 1.2 Inbound、Outbound 和端口分别是什么

- **Inbound（入站）**：互联网主动连接你的 VPS；需要严格限制。
- **Outbound（出站）**：VPS 主动访问外部，例如下载系统更新、拉取 GitHub/Docker 镜像、访问 ST 上游；通常保留默认允许。
- **来源 `/32`**：只允许一个 IPv4 地址。例如你的公网 IP 是 `198.51.100.25`，就填 `198.51.100.25/32`。

DigitalOcean Cloud Firewall 建议规则：

| 方向 | 协议/端口 | 实际用途 | 来源/目标 |
|---|---|---|---|
| Inbound | TCP 22 | SSH 远程管理 VPS | 仅你的当前公网 IP `/32`；IP 变化后先在控制台更新 |
| Inbound | TCP 80 | HTTP，nginx 只把它重定向到 HTTPS | 联调期可允许全部；稳定后只允许 Cloudflare IP 段 |
| Inbound | TCP 443 | Cloudflare 到源站 nginx 的 HTTPS | 联调期可允许全部；稳定后只允许 Cloudflare IP 段 |
| Outbound | All traffic | apt、GitHub、Docker、DNS、NTP、ST API | 全部目标（保留 DigitalOcean 默认出站规则） |

不要开放这些端口：

- `8001`：仅供 Compose 内部 nginx 访问 app，宿主机不需要开放；
- `5432`、`3306`：本项目使用本地 SQLite，没有公网数据库端口；
- Docker API `2375/2376`：不应暴露到互联网。

Cloudflare 开启橙云后，普通访客先连接 Cloudflare，再由 Cloudflare 连接 VPS 的 443。稳定运行后把 80/443
限制为 Cloudflare 官方全部 IPv4/IPv6 网段，可以阻止访客绕过 Cloudflare 直接打源站。不要漏掉部分网段，
否则不同地区可能间歇性出现 522。SSH 的 22 端口始终只允许你自己的 IP，不要填 Cloudflare IP。

> 如果你的家庭公网 IP 经常变化，先不要贸然收紧 22；可以每次变化后在 DigitalOcean 控制台更新 `/32`，
> 或后续使用 Tailscale/WireGuard 等管理网络。无论如何，都要保留 DigitalOcean 网页 Recovery Console 作为应急入口。

## 2. 首次登录与系统加固（含“已经使用密码登录”的情况）

### 2.1 为什么 Git clone 之前要做这些事

全新的公网 VPS 创建后几分钟内就可能收到自动扫描和 SSH 登录尝试。这里先做的事情不是项目配置，而是建立一个
可恢复、权限较小、打过安全补丁的主机基础：

| 操作 | 做了什么 | 为什么在拉代码前做 |
|---|---|---|
| `apt update` | 更新可安装软件索引 | 避免安装过期软件包 |
| `apt full-upgrade` | 安装系统和安全更新 | 先修补基础系统，再运行公网服务 |
| `adduser deploy` | 创建日常运维用户和独立 home | 不长期使用权限无限的 root |
| `usermod -aG sudo deploy` | 允许 `deploy` 在输入自己的密码后临时执行管理命令 | 普通操作低权限，危险操作显式使用 `sudo` |
| 安装 SSH 公钥 | 让服务器用密钥验证你的电脑 | 避免把可猜测/可撞库的密码作为公网 SSH 凭证 |
| 禁止 root 直登 | 阻止使用固定用户名 `root` 从公网直接登录 | 减少自动攻击面，也降低误操作风险 |
| 禁止密码 SSH | 只接受持有私钥的设备 | 阻止在线密码爆破；服务器不保存可用于登录的明文密码 |
| 自动安全更新/NTP | 自动安装重要更新并校准时间 | TLS、日志、JWT 和更新都依赖正确时间 |

禁止 root/密码 SSH **不是第一步，也不能在公钥验证前执行**。正确顺序是：保留当前 root 密码会话 → 创建
`deploy` → 安装公钥 → 另开窗口验证公钥和 sudo → 最后才关闭 root/密码远程登录。任何验证失败都不要继续。

### 2.2 你现在使用 root 密码登录：先保留原窗口

在你自己的电脑上连接（把示例 IP 换成真实 IP）：

```bash
ssh root@"$VPS_IP"
```

输入 DigitalOcean 创建 VPS 时设置/发送的 root 密码。**登录后不要关闭这个窗口**，它是迁移 SSH 配置失败时的
回退通道。然后在 VPS 上运行：

```bash
apt update
apt full-upgrade -y
adduser deploy
usermod -aG sudo deploy
```

`adduser deploy` 会要求设置一个 `deploy` 用户密码和若干资料；资料可直接回车跳过。这个密码用于 `sudo`，在
彻底关闭 SSH 密码认证后，仍然可以在已经登录的会话里用于 `sudo`。

### 2.3 在自己的电脑生成 SSH key

以下命令运行在**你自己的电脑**，不是 VPS：

```bash
ssh-keygen -t ed25519 -a 64 -C "digitalocean-st-imagen"
```

一路回车会保存到默认路径；建议为私钥设置 passphrase。生成的两个文件通常是：

```text
~/.ssh/id_ed25519      # 私钥：只留在自己的电脑，绝不能上传或发给任何人
~/.ssh/id_ed25519.pub  # 公钥：可以放到 VPS
```

macOS、Linux 或带 `ssh-copy-id` 的环境，运行：

```bash
ssh-copy-id deploy@"$VPS_IP"
```

它会暂时使用刚创建的 `deploy` 密码登录，然后把公钥追加到
`/home/deploy/.ssh/authorized_keys`。

Windows PowerShell 没有 `ssh-copy-id` 时，可运行：

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub | ssh deploy@<VPS_IP> "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"
```

也可以在仍打开的 root 会话里手动安装：

```bash
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
nano /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
```

`nano` 中只粘贴 `.pub` 公钥的**一整行**，不要粘贴没有 `.pub` 后缀的私钥。

### 2.4 必须另开窗口验证，再关闭密码登录

保留原 root 窗口，在自己电脑另开一个终端：

```bash
ssh -o PreferredAuthentications=publickey deploy@"$VPS_IP"
sudo -v
whoami
sudo whoami
```

预期：

```text
deploy
root
```

只有同时满足以下条件才继续：

- `deploy` 公钥登录成功；
- `sudo -v` 接受 `deploy` 用户密码；
- DigitalOcean Recovery Console 可以打开；
- 新终端保持登录，原 root 终端也暂时不要关闭。

然后在 **deploy 会话**创建配置：

```bash
sudoedit /etc/ssh/sshd_config.d/99-hardening.conf
```

写入：

```text
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
```

先检查语法；只有输出为空且退出码为 0 才重载：

```bash
sudo sshd -t
sudo systemctl reload ssh
```

再开第三个终端重新测试 `ssh deploy@$VPS_IP`。确认成功后，才可以退出最初的 root 密码会话。

> `reload` 不会主动踢掉已经建立的 SSH 会话，所以保留旧窗口很重要。如果新连接失败，立即在旧窗口撤销
> `/etc/ssh/sshd_config.d/99-hardening.conf`，或使用 DigitalOcean Recovery Console 修复。

### 2.5 如果暂时不想配置 SSH key

可以继续部署，但风险更高。至少应做到：

1. 创建 `deploy` 用户并使用密码管理器生成的长、唯一密码；
2. 先验证 `ssh deploy@$VPS_IP` 和 `sudo -v`；
3. 设置 `PermitRootLogin no`，只禁用 root 远程登录；
4. 暂时保留 `PasswordAuthentication yes`；
5. Cloud Firewall 的 TCP 22 只允许你的公网 IP `/32`；
6. 尽快完成第 2.3～2.4 节，再把 `PasswordAuthentication` 改为 `no`。

临时配置是：

```text
PermitRootLogin no
PasswordAuthentication yes
PubkeyAuthentication yes
```

如果你当前无法保证 22 端口来源 IP稳定，也没有确认 Recovery Console 可用，宁可先保留密码登录，也不要冒着
把自己锁在服务器外的风险直接复制“禁用密码”命令。

### 2.6 安装基础工具、自动更新并校准时间

在 `deploy` 会话运行：

```bash
sudo apt install -y unattended-upgrades ca-certificates curl git rsync
sudo dpkg-reconfigure -plow unattended-upgrades
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true
```

检查：

```bash
timedatectl status
git --version
curl --version
```

到这里才进入 Docker 和项目部署步骤。前两节只改主机账户、SSH、系统更新和网络入口，尚未接触项目代码。

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

```bash
sudo mkdir -p /opt/st-imagen
sudo chown deploy:deploy /opt/st-imagen
cd /opt/st-imagen
git clone <你的仓库 SSH/HTTPS 地址> .
git checkout main
mkdir -p data/uploads/generated data/backups deploy/certs
sudo chown -R 10001:10001 data
chmod 750 data
```

确认当前版本：

```bash
git rev-parse --short HEAD
```

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
install -m 600 /dev/null deploy/certs/origin.pem
install -m 600 /dev/null deploy/certs/origin.key
nano deploy/certs/origin.pem
nano deploy/certs/origin.key
```

粘贴时保留完整的 `BEGIN/END` 行。检查证书：

```bash
openssl x509 -in deploy/certs/origin.pem -noout -subject -issuer -dates
openssl pkey -in deploy/certs/origin.key -check -noout
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

先检查合并后的 Compose：

```bash
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml -f compose.4c8g.yml'
docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES config > /tmp/st-imagen.compose.yml
```

确认密钥文件存在且不可被其他用户读取：

```bash
test -s deploy/certs/origin.pem
test -s deploy/certs/origin.key
stat -c '%a %n' .env deploy/certs/origin.*
```

启动：

```bash
docker compose $COMPOSE_FILES up -d --build
docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=200 app nginx
```

新数据库会由应用初始化。已有数据库升级时执行：

```bash
docker compose $COMPOSE_FILES run --rm app alembic upgrade head
sudo chown -R 10001:10001 data
```

如果是迁移前就存在、但从未接入 Alembic 的旧数据库，请先备份，再按项目 README 的基线版本执行
`alembic stamp`；不要对未知版本数据库盲目 stamp。

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

在阿里云安全组开放策略与 DigitalOcean 相同：

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
chmod 600 .env deploy/certs/origin.key deploy/certs/origin.pem
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

## 官方参考

- DigitalOcean Droplet 创建与基础操作：<https://docs.digitalocean.com/products/droplets/how-to/create/>
- DigitalOcean Cloud Firewalls：<https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/>
- Docker Engine on Ubuntu：<https://docs.docker.com/engine/install/ubuntu/>
- Docker Linux post-install：<https://docs.docker.com/engine/install/linux-postinstall/>
- Cloudflare 代理状态：<https://developers.cloudflare.com/dns/proxy-status/>
- Cloudflare Full (strict)：<https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/>
- Cloudflare Origin CA：<https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
- Cloudflare IP ranges：<https://www.cloudflare.com/ips/>
