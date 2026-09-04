# 2C2G VPS + Cloudflare 完整部署手册

本文适用于在 Ubuntu **2 vCPU / 2 GiB** VPS 上长期运行，并由 Cloudflare 管理域名 DNS 和公网 TLS。适用于阿里云及其他提供标准 Ubuntu 的云厂商。

> 本项目必须保持 `UVICORN_WORKERS=1`。账号槽位、全局准入、限流和熔断状态均在进程内。仓库的 `compose.prod.yml` 已包含 2C2G 资源限制。

## 0. 部署拓扑与文件

```text
浏览器 → Cloudflare（DNS/CDN/TLS）→ VPS:443 nginx → app:8001
                                      ├─ /static 直接读取仓库静态文件
                                      └─ /uploads 直接读取 data/uploads
```

部署只组合两个 Compose 文件：

- `compose.prod.yml`：2C2G 生产基线；
- `compose.cloudflare.yml`：Cloudflare Origin CA TLS。

仓库不再保留额外的大规格资源覆盖文件，首次部署、更新、恢复和排障始终使用上述两个文件。进入项目目录后，用下面的环境变量缩短命令：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
```

`COMPOSE_FILES` 只在当前 Shell 有效；每次重新 SSH 登录或新开 Shell 都要重新执行。变量值有意不加引号传给 `docker compose`，让 Bash 将两个 `-f` 参数正确拆开；不要写成 `docker compose "$COMPOSE_FILES"`。后续可独立复制的关键命令块会重复导出，避免遗漏。

服务器私有文件：

- `.env`：密钥和运行参数，不提交 Git；
- `deploy/certs/origin.pem`、`deploy/certs/origin.key`：Cloudflare Origin CA 证书和私钥；
- `data/`：SQLite、参考图、生成图和备份，是迁移时必须复制的持久化目录。

## 1. 创建通用云 VPS（使用 SSH key）

本节不绑定具体厂商。阿里云 ECS、腾讯云 CVM 或其他提供标准 Ubuntu 的 VPS 都可以；控制台里的“安全组”“云防火墙”“防火墙规则”名称不同，但作用相同。

### 1.1 在自己的电脑准备 SSH key

SSH key 用于证明登录者持有私钥，比开放 root 密码登录更安全。**私钥只保存在自己的电脑或密码管理器中，不能上传到 VPS、Git 仓库或聊天记录。**

如果 Termius 或本机已有专用 Ed25519 key，可以继续使用；否则在自己的电脑执行：

```bash
# 生成 Ed25519 密钥；-a 64 增加私钥口令派生轮数，-C 只是便于识别的备注。
ssh-keygen -t ed25519 -a 64 -C "st-imagen-vps"

# 输出公钥。只把这一行添加到云厂商控制台，绝不要输出或上传无 .pub 后缀的私钥。
cat ~/.ssh/id_ed25519.pub
```

创建实例时选择“SSH 密钥/密钥对/公钥认证”，导入 `.pub` 内容并绑定实例，不选择密码认证。阿里云可在 ECS 控制台的“网络与安全 → 密钥对”创建或导入密钥对，再绑定到实例。

### 1.2 通用实例参数

- 操作系统：Ubuntu 24.04 LTS x64；
- 规格：2 vCPU / 2 GiB；
- 系统盘：至少 50 GiB，并开启云盘快照或自动备份；
- 网络：分配固定公网 IPv4（不同厂商可能叫弹性公网 IP、EIP 或 Reserved IP）；
- 地域：选择主要用户和上游 API 网络延迟都较低的区域；
- 登录：绑定第 1.1 节确认过的 SSH 公钥。

实例创建后，在**自己的电脑终端**记录本次会话会用到的值：

```bash
# 替换为 VPS 的固定公网 IPv4。
export VPS_IP="203.0.113.10"
# 替换为准备在 Cloudflare 中使用的完整域名。
export DOMAIN="img.example.com"
```

`export` 只在当前终端会话有效；关闭终端或重新登录后需要再次执行。可用 `printf '%s\n' "$VPS_IP" "$DOMAIN"` 检查是否设置正确。

### 1.3 配置厂商安全组/云防火墙

云安全组在流量到达 Ubuntu 之前过滤连接；Ubuntu UFW 是主机内第二层防护；应用登录认证是第三层。三者不能互相替代。

- 入方向 TCP 22：只允许你当前的运维公网 IP `/32`；运维 IP 改变时先更新规则；
- 入方向 TCP 80：联调时可临时允许公网，稳定后只允许 Cloudflare 官方 IPv4/IPv6 网段；
- 入方向 TCP 443：联调时可临时允许公网，稳定后只允许 Cloudflare 官方 IPv4/IPv6 网段；
- 出方向：允许 DNS、NTP、Ubuntu 软件源、GitHub、Docker Registry 和上游 API 所需流量。

不要开放这些端口到公网：8001、5432、3306、2375、2376。开始系统加固前，先确认云厂商的 VNC/Workbench/救援终端或串行控制台可用，以便 SSH 配置错误时恢复；22 端口应允许你的运维 IP，而不是 Cloudflare IP。

## 2. 首次登录、建立运维账号并加固系统

本节的目标是：先用云厂商注入的 key 完成一次管理员登录，创建日常使用的 `deploy` 账号，验证第二条可用的 SSH 通道，最后才关闭 root 与密码登录。**顺序不能颠倒，否则可能把自己锁在服务器外。**

### 2.1 从自己的电脑首次登录

以下命令在自己的电脑或 Termius 本地终端执行，不是在 VPS 的 Shell 中执行：

```bash
# 只允许公钥认证，连接云厂商初始管理员；Ubuntu 镜像也可能要求 ubuntu 用户。
ssh -o PreferredAuthentications=publickey root@"$VPS_IP"
```

如果厂商镜像禁止 root，请按控制台说明改用 `ubuntu@"$VPS_IP"`，并在后续管理命令前加 `sudo`。登录失败时应检查实例绑定的公钥、本机选择的私钥和安全组 22 端口，不要为了省事启用弱密码。

### 2.2 更新系统并创建 deploy 用户

登录 VPS 后执行下面一组命令。先保持这个初始管理员窗口开启，直到第 2.3 节的新窗口验证完成。

```bash
# 刷新 Ubuntu 软件包索引，不会安装软件。
sudo apt update
# 安装当前可用的系统和安全更新；生产部署前先完成基础修补。
sudo apt full-upgrade -y
# 创建日常运维账号；命令会交互询问密码和备注，密码仅作为本机 sudo 后备。
sudo adduser deploy
# 把 deploy 加入 sudo 组，使其可在认证后执行系统管理命令。
sudo usermod -aG sudo deploy
# 创建 deploy 的 SSH 配置目录，并设置只有该用户可进入的 0700 权限。
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
# 复制云厂商已注入的公钥授权文件；0600 防止其他用户修改登录 key。
sudo install -m 600 -o deploy -g deploy \
  ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
# 显示目录和文件的属主/权限，预期分别是 deploy:deploy 700 和 600。
sudo stat -c '%U:%G %a %n' /home/deploy/.ssh /home/deploy/.ssh/authorized_keys
```

如果最初以 root 登录，`~/.ssh/authorized_keys` 指向 `/root/.ssh/authorized_keys`；如果以 `ubuntu` 登录，则指向 `/home/ubuntu/.ssh/authorized_keys`，所以上述写法兼容两种镜像。

### 2.3 在第二个本地窗口验证 deploy

不要从 VPS 连接它自己，因为服务器没有也不应该保存你的私钥。在自己的电脑另开终端或 Termius 标签页：

```bash
# IdentitiesOnly 防止 SSH 依次尝试其他 key；-i 指定与云实例公钥配对的本机私钥。
ssh -o IdentitiesOnly=yes -o PreferredAuthentications=publickey \
  -i ~/.ssh/id_ed25519 deploy@"$VPS_IP"
```

如果 Termius 已在 Host 配置中选择了正确 Identity，可以省略 `-i`。进入新会话后：

```bash
# 提前验证并缓存 sudo 凭据；失败时不要继续禁用旧登录方式。
sudo -v
# 应输出 deploy，确认当前不是 root 会话。
whoami
# 应输出 root，确认 deploy 的 sudo 权限可用。
sudo whoami
```

只有 deploy 公钥登录成功、`sudo whoami` 输出 `root`、云厂商救援控制台可用时，才进入下一步。此时仍保留最初的管理员窗口。

### 2.4 最后禁用 root 和密码 SSH 登录

在已经验证成功的 deploy 会话执行：

```bash
# 用安全编辑器创建优先级较高的 sshd 配置片段。
sudoedit /etc/ssh/sshd_config.d/00-st-imagen-hardening.conf
```

写入：

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

然后逐项验证并平滑重载：

```bash
# 只检查语法；没有输出且退出码为 0 才能继续。
sudo sshd -t
# 显示 sshd 最终合并后的认证配置，确认与上面四项一致。
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
# reload 不会踢掉当前连接，并让新连接使用加固后的配置。
sudo systemctl reload ssh
```

OpenSSH 多数认证项采用先读到的值，`00-st-imagen-hardening.conf` 应排在 Ubuntu 的 `50-cloud-init.conf` 前。再开第三个本地窗口执行 `ssh deploy@"$VPS_IP"`；确认仍能登录后，才退出最初的管理员会话。

### 2.5 安装基础工具并校准时间

```bash
# 安装自动安全更新、证书、下载、Git 和迁移所需工具。
sudo apt install -y unattended-upgrades ca-certificates curl git rsync
# 交互启用 Ubuntu unattended-upgrades，自动应用安全更新。
sudo dpkg-reconfigure -plow unattended-upgrades
# 服务器统一使用 UTC，避免日志、备份文件名和数据库时间混乱。
sudo timedatectl set-timezone UTC
# 启用网络时间同步，避免 OAuth、JWT 和 TLS 因时钟漂移失败。
sudo timedatectl set-ntp true
# 检查时区、NTP 服务和系统时钟状态。
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
git switch main
git pull --ff-only origin main
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
test -d .git || { echo "错误：当前目录不是 Git 仓库"; exit 1; }
test -f compose.prod.yml || { echo "错误：缺少 compose.prod.yml"; exit 1; }
sudo test -d data/uploads/generated || { echo "错误：缺少 uploads/generated"; exit 1; }
git rev-parse --short HEAD
sudo ls -ldn data data/uploads data/uploads/generated
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  test -w data/uploads/generated \
  && echo "app UID 10001 can write generated uploads"
```

`pwd` 必须是 `/opt/st-imagen`，而不是 `/opt/st-imagen/st-imagen`。这里对子目录使用
`sudo` 是有意的：上一段已将 `data` 设为 UID 10001 所有、mode `750`，普通 deploy 用户
不能穿过它查看数据库或 uploads；这不影响 app 写入和 nginx 的独立只读挂载。这里用
`setpriv` 模拟容器的数值 UID/GID，因为宿主机 `/etc/passwd` 没有名为 `10001` 的用户，
部分 sudo 配置会拒绝 `sudo -u '#10001'` 并报告 `unknown user #10001`。

## 5. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

先构建镜像，以便使用镜像内的 `cryptography` 生成 Fernet key：

```bash
docker compose -f compose.prod.yml build app
FERNET_KEY=$(docker compose -f compose.prod.yml run --rm --no-deps app \
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
USER_SESSION_COOKIE_NAME=imagen_session
CORS_ORIGINS=https://img.example.com
CORS_ALLOW_CREDENTIALS=true
ST_TRUST_ENV=false
```

注意：

- 不要修改 Fernet key；否则数据库内已有 API key 无法解密；
- `ADMIN_PATH` 只是降低入口曝光，不替代管理员认证；
- 同源部署可把 `CORS_ORIGINS` 留空；若填写，必须是精确 HTTPS Origin，末尾不加 `/`；
- 2C2G 部署必须保持单 worker，不要通过增加 worker 绕过进程内并发控制；
- `GENERATION_GLOBAL_MAX_CONCURRENT` 和各账号 `max_inflight` 仍应按上游承载能力压测后设置，CPU 变多不代表上游额度变多。

`ST_BASE_URL` 必须是上游 API 根地址，不是本站域名、网页首页、示例占位符或具体
`/inference/...` 路径。保存配置后继续第 6 节；如果后台测试返回 502，再查第 12.5 节，
不要在正常部署流程中提前启动半配置状态的容器。

### 5.1 后续修改 `.env` 后应用新参数

首次部署继续执行第 6～7 节即可。站点已经运行后，如果再次修改 `.env`，单纯执行
`docker compose restart` **不会更新旧容器的环境变量**；必须让 Compose 重新创建 app
容器：

```bash
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES up -d --force-recreate app
docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=100 app
```

例如修改 `ACCOUNT_MAX_INFLIGHT=10` 后，验证容器实际获得的新值：

```bash
docker compose $COMPOSE_FILES exec app sh -c \
  'printf "ACCOUNT_MAX_INFLIGHT=%s\\n" "$ACCOUNT_MAX_INFLIGHT"'
```

预期输出 `ACCOUNT_MAX_INFLIGHT=10`。该默认值只影响之后新增或批量导入的账号；数据库中
已经存在且并发为 2 的账号不会被环境变量自动改写，需在后台编辑账号。批量导入接口本身
现在也固定以 10 作为未显式指定时的默认并发。

## 6. 在 Cloudflare 创建 DNS 与 Origin CA 证书

### 6.1 DNS

Cloudflare → **DNS → Records**：

- Type：`A`；
- Name：例如 `img`；
- IPv4：VPS 的固定公网 IPv4（EIP/弹性公网 IP）；
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
```

`install` 成功时不会输出任何内容；它只是创建两个 root 所有的空文件。不要在 Git 仓库内
对这些文件使用 `sudoedit`：其安全检查会因为父目录可由 deploy 写入而拒绝，并显示
`editing files in a writable directory is not permitted`。

先执行下面一条命令，终端会等待输入。粘贴 Cloudflare 的完整 **Origin Certificate**
（包括 `BEGIN CERTIFICATE` / `END CERTIFICATE`），然后单独按一次 `Ctrl+D` 保存：

```bash
sudo tee deploy/certs/origin.pem >/dev/null
```

再执行下面一条，粘贴完整 **Private Key**（包括私钥的 `BEGIN` / `END` 行），按
`Ctrl+D` 保存：

```bash
sudo tee deploy/certs/origin.key >/dev/null
```

最后统一确认所有权和权限：

```bash
sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key
sudo chmod 644 deploy/certs/origin.pem
sudo chmod 600 deploy/certs/origin.key
sudo test -s deploy/certs/origin.pem || { echo "错误：origin.pem 为空"; exit 1; }
sudo test -s deploy/certs/origin.key || { echo "错误：origin.key 为空"; exit 1; }
```

粘贴时保留完整的 `BEGIN/END` 行。证书本身不含私钥，可以是 `644`；私钥必须保持
`600`。这里特意让两者归 `root:root`：生产 Compose 删除了 nginx 的文件权限绕过
能力，若文件归普通 `deploy` 用户且为 `600`，容器内 nginx 主进程即使是 UID 0 也可能
收到 `Permission denied`。检查证书：

```bash
sudo openssl x509 -in deploy/certs/origin.pem -noout -subject -issuer -dates
sudo openssl pkey -in deploy/certs/origin.key -check -noout
sudo stat -c '%U:%G %a %s bytes %n' deploy/certs/origin.pem deploy/certs/origin.key
```

Cloudflare → **SSL/TLS → Overview** 设置为 **Full (strict)**，不要使用 Flexible。随后可开启：

- Edge Certificates → Always Use HTTPS；
- Edge Certificates → Automatic HTTPS Rewrites；
- HSTS 仅在确认域名及所有需要的子域都能长期 HTTPS 后开启，避免误锁站点。

建议添加 Cache Rules：

1. `/api/*`、随机后台路径、`/health*`：Bypass cache；
2. `/uploads/*`：尊重源站 `Cache-Control`；
3. 不要对整个站点使用 “Cache Everything”，否则登录态/API 可能被错误缓存。

## 7. 校验并启动 2C2G 部署

```bash
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES config > /tmp/st-imagen.compose.yml
sudo stat -c '%U:%G %a %n' deploy/certs/origin.pem deploy/certs/origin.key
docker compose $COMPOSE_FILES run --rm --no-deps nginx nginx -t
docker compose $COMPOSE_FILES build --pull app
docker compose $COMPOSE_FILES run --rm --no-deps app alembic upgrade head
docker compose $COMPOSE_FILES up -d --force-recreate --remove-orphans app nginx
docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=200 app nginx
```

证书预期 `root:root 644`，私钥预期 `root:root 600`。Alembic 命令对新数据库和已有数据库都可重复安全执行，不要等容器启动后才补做迁移。

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
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES exec app sh -c 'echo workers=$UVICORN_WORKERS http=$HTTP_MAX_CONNECTIONS downloads=$GENERATED_IMAGE_DOWNLOAD_CONCURRENCY db=$DB_POOL_SIZE'
```

2C2G 预期：app 上限约 1.6 CPU、1400 MiB；nginx 上限约 0.4 CPU、300 MiB；宿主机仍保留资源给 Docker、文件缓存和 SSH。

## 9. 日常更新、监控与备份

`COMPOSE_FILES` 可以显著缩短命令，但每次 SSH 登录后必须重新导出。下面的备份和更新命令块都包含该导出语句，可以分别整段复制执行。

更新前先确认仓库干净并备份：

```bash
set -euo pipefail
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
test -d .git
test -z "$(git status --porcelain)" || { echo "工作区有未提交修改，停止更新"; exit 1; }
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose $COMPOSE_FILES exec -T app \
  python scripts/backup_data.py --include-uploads
sudo tar --acls --xattrs -C /opt -czf "/home/deploy/st-imagen-$STAMP.tgz" \
  st-imagen/data st-imagen/.env st-imagen/deploy/certs
sudo chown deploy:deploy "/home/deploy/st-imagen-$STAMP.tgz"
chmod 600 "/home/deploy/st-imagen-$STAMP.tgz"
```

然后拉取明确的 `origin/main`、构建新镜像、执行迁移并重建两个服务：

```bash
set -euo pipefail
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
git fetch origin --prune
git switch main
git pull --ff-only origin main

docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES pull nginx
docker compose $COMPOSE_FILES build --pull app
docker compose $COMPOSE_FILES run --rm --no-deps app \
  alembic upgrade head
docker compose $COMPOSE_FILES up -d --force-recreate --remove-orphans app nginx
docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=100 app nginx
curl -fsS "https://$DOMAIN/health/ready"
```

这里显式写 `origin main`，避免本地分支没有 upstream、错误跟踪功能分支或远程默认分支变化时，裸 `git pull --ff-only` 拉错目标。`--no-deps` 防止数据库迁移命令提前启动旧依赖，迁移成功后才统一重建 app 和 nginx；这样 nginx 配置或镜像有更新时也不会被漏掉。

常用排障：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES logs -f --tail=200 app nginx
docker stats
df -h /opt/st-imagen/data
sudo journalctl -u docker --since '1 hour ago'
```

归档包含 `.env` 和 TLS 私钥，必须在上传对象存储前使用 `age`、GPG 或等效工具加密，并在完成恢复/传输后删除服务器上的临时明文归档。建议至少每天把 `data/` 的加密备份同步到独立对象存储；只有同一 VPS 上的备份不具备灾难恢复能力。定期做恢复演练，而不只是确认备份文件存在。

## 10. 2C2G 资源确认与迁移恢复

日常部署始终只使用 `compose.prod.yml` 和 `compose.cloudflare.yml`。更新后重建现有容器，确认 2C2G 资源限制已经落到容器：

```bash
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES config --quiet
docker inspect st-imagen-app --format 'CPU={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'
```

预期 app 为 1.6 CPU、1400 MiB。若从旧服务器迁移，只复制同一 Git commit 对应的 `data/`、`.env` 和 `deploy/certs/`，恢复权限后按第 7 节启动；切换 DNS 前必须停止旧站写入，不能让两个 SQLite 实例同时接收业务后再尝试合并。

## 11. 回滚

应用更新失败：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
git checkout <上一个已验证 commit>
docker compose $COMPOSE_FILES up -d --build
```

迁移失败：把 Cloudflare A 记录切回旧服务器 IP，并用相同的两个 Compose 文件重新启动旧机。不要让新旧两台同时接受写入后再合并 SQLite。

## 12. 集中故障排查

正常部署只按 1～11 节顺序执行。本节只在出现对应错误时使用，不要把修复命令混入首次部署。

### 12.1 SSH 握手成功但 root 密码失败

新建 VPS 应使用第 1 节 SSH key 流程。旧密码机出现 `Authentication failed (password)` 时，
22 端口和 sshd 已经可达；通过云厂商控制台的“重置密码”或 VNC/Workbench/救援终端恢复。恢复后检查：

```bash
passwd -S root
chage -l root
sshd -T -C user=root,host="$(hostname)",addr="你的当前公网IP" \
  | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
```

只有 root 密码时不要跳过 2.3 和 2.4 后又执行禁用 root/password 的命令。紧急临时文件可用
`00-emergency-recovery.conf` 设置 `PermitRootLogin yes`、`PasswordAuthentication yes`，但
验证 deploy key 后必须删除。若存在陌生登录/公钥或密码自行变化，把 VPS 视为已失陷并
重建、轮换所有密钥。不要长期退回密码部署。

### 12.2 deploy 报 `Permission denied (publickey)`

先确认测试发起位置：如果命令前的提示符是 `root@ubuntu...#`，你是在 VPS 上错误地连接
VPS 自己；退出这个测试，在本机 Termius 新标签页选择原私钥后连接。首次出现主机指纹询问
并不代表公钥认证成功，它只是在确认服务器身份。

若测试确实来自自己的电脑，保留 VPS root 窗口，在 VPS 修复公钥文件：

```bash
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
install -m 600 -o deploy -g deploy \
  /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy /home/deploy/.ssh /home/deploy/.ssh/authorized_keys
chmod 755 /home/deploy
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
namei -l /home/deploy/.ssh/authorized_keys
sshd -T -C user=deploy,host="$(hostname)",addr="你的当前公网IP" \
  | grep -E '^(pubkeyauthentication|authorizedkeysfile) '
journalctl -u ssh --since "10 minutes ago" --no-pager | tail -n 100
```

随后在**本机**使用详细日志和明确私钥重试：

```bash
ssh -vvv -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 deploy@"$VPS_IP"
```

不要把 `id_ed25519` 私钥复制到服务器。仍失败时检查 Termius 选择的 Keychain 条目是否就是
创建 VPS 时绑定公钥所对应的私钥，而不是另一个同名或旧密钥。

### 12.3 仓库目录错误

`fatal: not a git repository` 时：

```bash
find /opt/st-imagen -maxdepth 3 -type d -name .git -print
```

若看到 `/opt/st-imagen/st-imagen/.git`，说明旧命令克隆了嵌套目录；全新且无数据时删除后按
第 4 节重新克隆。已有 `.env`、证书或数据时先备份，不能直接 `rm -rf`。

### 12.4 `data` 权限或图片 `Permission denied`

如果仅在 deploy Shell 执行普通
`ls -ldn data data/uploads data/uploads/generated` 时看到
`ls: cannot access 'data/uploads': Permission denied`，而 `sudo ls` 正常，这是第 4 节
`data=750` 最小权限的预期结果，不需要修改权限。使用：

```bash
sudo ls -ldn data data/uploads data/uploads/generated
sudo setpriv --reuid=10001 --regid=10001 --clear-groups \
  test -w data/uploads/generated && echo writable
```

只有 nginx 日志或 app 写入也出现拒绝时，才执行下面的修复。

`chmod: ... Operation not permitted` 是宿主机 deploy 已不再拥有 UID 10001 文件。
存储计数增加但 nginx 日志出现
`open() "/srv/uploads/generated/gen-....jpg" failed (13: Permission denied)` 时执行：

```bash
cd /opt/st-imagen
sudo chown -R 10001:10001 data
sudo chmod 750 data
sudo find data/uploads -type d -exec chmod 755 {} +
sudo find data/uploads -type f -exec chmod 644 {} +
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
IMAGE=$(sudo find data/uploads/generated -maxdepth 1 -type f -printf '%f\n' | head -n 1)
docker compose $COMPOSE_FILES exec --user 101 nginx test -r "/srv/uploads/generated/$IMAGE"
```

不要对整个 `data` 执行 `chmod -R 755`；数据库仍需保护。权限立即生效，无需重启容器。

### 12.5 登录正常但生图 502 非 JSON

确认 `.env` 的 `ST_BASE_URL` 是真实上游 API 根地址，不是本站域名、
`https://upstream.example.com` 或具体 inference 路径。修改后必须 `--force-recreate app`。
以下检查不会输出 API key：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
docker compose $COMPOSE_FILES exec app python - <<'UPSTREAMPY'
import os
from urllib.parse import urlsplit
value = os.environ.get("ST_BASE_URL", "")
parsed = urlsplit(value)
print(value, parsed.scheme, parsed.hostname, parsed.path or "/")
UPSTREAMPY
```

错误中的 HTTP 状态、Content-Type 和大小可区分 HTML 拦截页与空响应，不要公开响应正文。

### 12.6 nginx 证书不可读

`cannot load certificate ... Permission denied`：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key
sudo chmod 644 deploy/certs/origin.pem
sudo chmod 600 deploy/certs/origin.key
docker compose $COMPOSE_FILES run --rm --no-deps nginx nginx -t
```

### 12.7 nginx 临时目录不能 chown

`chown("/var/cache/nginx/client_temp", 101) failed` 表示旧版 Compose 缺 capability。更新代码，
确认 `compose.prod.yml` 的 `cap_add` 包含 `CHOWN`、`NET_BIND_SERVICE`、`SETGID`、`SETUID`，然后：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
git fetch origin --prune
git switch main
git pull --ff-only origin main
docker compose $COMPOSE_FILES up -d --force-recreate nginx
```

不要使用 `chmod 777` 或 `privileged: true`。

## 官方参考

- Docker Engine on Ubuntu：<https://docs.docker.com/engine/install/ubuntu/>
- Docker Linux post-install：<https://docs.docker.com/engine/install/linux-postinstall/>
- Cloudflare 代理状态：<https://developers.cloudflare.com/dns/proxy-status/>
- Cloudflare Full (strict)：<https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/>
- Cloudflare Origin CA：<https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
- Cloudflare IP ranges：<https://www.cloudflare.com/ips/>
