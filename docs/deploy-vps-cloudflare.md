# 2C2G VPS + Cloudflare 完整部署手册

本文适用于在 Ubuntu **2 vCPU / 2 GiB** VPS 上长期运行，并由 Cloudflare 管理域名 DNS 和公网 TLS。DigitalOcean、阿里云或其他提供标准 Ubuntu 的云厂商均可使用。

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

仓库不再保留额外的大规格资源覆盖文件，首次部署、更新、恢复和排障始终使用上述两个文件。为缩短后续命令，在进入仓库后定义：

```bash
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
```

这里故意不加引号使用 `$COMPOSE_FILES`，让 Bash 把两个 `-f` 参数展开成独立参数。`export` 只在当前 Shell 会话有效；每次重新 SSH 登录或新开终端，都要重新执行这一行。不要把值写进 `.env`，因为它是 Shell 命令参数，不是容器环境变量。

服务器私有文件：

- `.env`：密钥和运行参数，不提交 Git；
- `deploy/certs/origin.pem`、`deploy/certs/origin.key`：Cloudflare Origin CA 证书和私钥；
- `data/`：SQLite、参考图、生成图和备份，是迁移时必须复制的持久化目录。

## 1. 创建通用云服务器（使用 SSH key）

本节不绑定任何云厂商。阿里云 ECS、腾讯云 CVM、DigitalOcean Droplet，以及其他提供 Ubuntu
公网 VPS 的厂商都可以使用；控制台里的“安全组”“云防火墙”“弹性公网 IP”等名称可能不同，
作用是一样的。

### 1.1 在自己的电脑准备 SSH 密钥

如果 Termius 或本机已经有专用于这台服务器的 Ed25519 密钥，可以直接使用；否则在**自己的
电脑**生成：

```bash
ssh-keygen -t ed25519 -a 64 -C "st-imagen-vps"
```

- `-t ed25519`：使用现代 Ed25519 密钥类型；
- `-a 64`：提高私钥口令派生轮数，降低私钥文件泄露后的破解速度；
- `-C`：写入便于识别的注释，不参与认证。

命令会生成私钥和以 `.pub` 结尾的公钥。**私钥只留在自己的设备**；在云厂商创建实例时，
只上传或粘贴 `.pub` 公钥。若控制台可选择认证方式，应选择 SSH 密钥而不是密码。

### 1.2 创建实例

建议参数：

- Ubuntu 24.04 LTS x64；
- 2 vCPU / 2 GiB，系统盘至少 50 GiB；
- 分配固定公网 IPv4（厂商可能称“弹性公网 IP”“EIP”或“保留 IP”）；
- 选择主要用户和上游 API 延迟较低的区域；
- 创建时绑定上一步的 SSH 公钥；
- 确认云控制台提供 VNC、串口或救援模式，以便 SSH 配置错误时恢复。

在自己的电脑记录实际地址；`export` 只对当前终端会话生效，新开终端需要重新执行：

```bash
export VPS_IP="203.0.113.10"
export DOMAIN="img.example.com"
```

### 1.3 配置云防火墙或安全组

云防火墙/安全组在流量到达 Ubuntu 之前过滤连接；Ubuntu 自身防火墙和应用登录认证是另外两层，
不能互相替代。不同厂商界面名称不同，请创建等价规则：

- 入站 TCP 22：只允许你的运维公网 IP `/32`；IP 变化时先更新规则；
- 入站 TCP 80：用于 HTTP 跳转或证书联调；联调期可临时允许全部，稳定后只允许 Cloudflare 官方网段；
- 入站 TCP 443：用于 Cloudflare 回源 HTTPS；联调期可临时允许全部，稳定后只允许 Cloudflare 官方网段；
- 出站流量：允许访问软件源、GitHub、Docker Registry、DNS、NTP 和上游 API；
- 不要对公网开放 8001、5432、3306、2375、2376。

22 端口必须允许你的运维 IP，而不是 Cloudflare IP。完成后先确认云控制台的救援入口可用，再继续
修改 SSH 配置。

## 2. 首次登录、创建运维用户并加固 SSH

本节的目标是：先保留一个可恢复的 root 会话，创建日常使用的 `deploy` 用户，独立验证新用户的
公钥和 sudo，最后才关闭 root 登录和密码登录。顺序不能颠倒，否则可能把自己锁在服务器外。

### 2.1 从自己的电脑首次登录

在**自己的电脑或 Termius**执行；不要在 VPS 的 Shell 里让服务器 SSH 连接自己：

```bash
ssh -o PreferredAuthentications=publickey root@"$VPS_IP"
```

`ssh` 建立远程会话；`PreferredAuthentications=publickey` 要求优先使用云实例创建时绑定的公钥，
`root@"$VPS_IP"` 指定初始管理员和服务器地址。若厂商默认用户不是 root（例如 `ubuntu`），请按
控制台说明替换用户名，并在后续需要管理员权限的命令前加 `sudo`。

登录成功后保持这个窗口不要关闭，依次执行：

```bash
apt update
apt full-upgrade -y
adduser deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
install -m 600 -o deploy -g deploy \
  /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
```

各命令作用：

1. `apt update`：刷新 Ubuntu 软件包索引，不会直接升级软件；
2. `apt full-upgrade -y`：安装安全更新，并允许按依赖需要增删包；若提示需要重启，完成本节验证后再重启；
3. `adduser deploy`：创建日常运维账号及其 home 目录；交互式资料可留空，但密码仍应设置为强随机值；
4. `usermod -aG sudo deploy`：把 deploy **追加**到 sudo 组；`-a` 不可省略，否则会覆盖其他附加组；
5. 第一条 `install`：创建只有 deploy 可进入的 `.ssh` 目录，同时一次设置 owner、group 和 `0700` 权限；
6. 第二条 `install`：把当前已验证的公钥授权复制给 deploy，并设置 owner 和 `0600` 权限；它复制的是公钥列表，不是私钥。

### 2.2 在第二个本机窗口验证 deploy

保留原 root 窗口。在自己的电脑另开终端/Termius 标签页，选择同一把私钥并执行：

```bash
ssh -o IdentitiesOnly=yes -o PreferredAuthentications=publickey \
  -i ~/.ssh/id_ed25519 deploy@"$VPS_IP"
```

- `IdentitiesOnly=yes`：只尝试明确选择的密钥，避免 SSH agent 中密钥过多导致认证失败；
- `-i`：指定本机私钥路径；Termius 已在界面绑定 Identity 时可省略；
- `deploy@...`：验证之后真正用于维护服务器的非 root 账号。

进入新会话后执行：

```bash
whoami
sudo -v
sudo whoami
```

`whoami` 应输出 `deploy`；`sudo -v` 验证并缓存 sudo 凭据；`sudo whoami` 应输出 `root`。只有三项都
符合预期，才能继续关闭 root/password 登录。

### 2.3 最后关闭 root 和密码登录

在已经验证成功的 deploy 会话执行：

```bash
sudoedit /etc/ssh/sshd_config.d/00-st-imagen-hardening.conf
```

`sudoedit` 用普通用户的编辑器安全修改 root 文件。写入：

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

依次验证并平滑加载配置：

```bash
sudo sshd -t
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
sudo systemctl reload ssh
```

- `sshd -t`：只检查语法，失败时绝不能 reload；
- `sshd -T`：显示最终生效配置，确认 root、密码和键盘交互认证均关闭，公钥认证开启；
- `systemctl reload ssh`：不中断现有连接地加载新配置，比 restart 更适合远程加固。

Ubuntu 的 SSH 配置可能采用“先读到的值生效”，所以文件名前缀使用 `00-`，确保它排在
`50-cloud-init.conf` 前。保留现有窗口，再从自己的电脑开第三个窗口测试 `ssh deploy@"$VPS_IP"`；
成功后才退出最初的 root 会话。

### 2.4 安装基础运维工具并启用自动安全更新

```bash
sudo apt install -y unattended-upgrades ca-certificates curl git rsync
sudo dpkg-reconfigure -plow unattended-upgrades
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true
timedatectl status
```

- `apt install`：安装自动安全更新、CA 证书、下载、Git 和文件同步工具；
- `dpkg-reconfigure`：交互确认启用 unattended-upgrades；
- `set-timezone UTC`：统一服务器、日志和备份时间基准；
- `set-ntp true`：启用网络校时，避免 TLS、Token 和日志因时钟漂移异常；
- `timedatectl status`：确认时区、NTP 服务和系统时钟状态。

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
- IPv4：VPS 绑定的固定公网 IPv4/EIP；
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

不能在 app 启动前使用 `run --no-deps nginx nginx -t`：Nginx 解析配置时会立即解析
`app:8001`，而 `--no-deps` 既不启动 app，也不会为一次性容器提供可解析的 app 服务，因而会报
`host not found in upstream "app:8001"`。正确顺序是先构建和迁移，再启动 app，最后让 Compose
带上依赖执行 Nginx 校验并启动正式 Nginx：

```bash
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'

# 1. 检查 Compose 合并配置与证书权限
docker compose $COMPOSE_FILES config --quiet
docker compose $COMPOSE_FILES config > /tmp/st-imagen.compose.yml
sudo stat -c '%U:%G %a %n' deploy/certs/origin.pem deploy/certs/origin.key

# 2. 构建 app
docker compose $COMPOSE_FILES build --pull app

# 3. 在一次性 app 容器中执行数据库迁移
docker compose $COMPOSE_FILES run --rm --no-deps app alembic upgrade head

# 4. 先启动 app；Nginx 校验需要通过 Compose 网络解析 app
docker compose $COMPOSE_FILES up -d --force-recreate app

# 5. 不加 --no-deps，让 Compose 确认 app 已健康后再校验 Nginx
docker compose $COMPOSE_FILES run --rm nginx nginx -t

# 6. 启动正式 Nginx，并清理已经从 Compose 删除的孤儿容器
docker compose $COMPOSE_FILES up -d --force-recreate --remove-orphans nginx

docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=200 app nginx
```

`run --rm nginx nginx -t` 不发布正式 Nginx 的 80/443 端口，只创建校验用的一次性容器；因为没有
`--no-deps`，Compose 会按 `depends_on` 等待 app 健康并把校验容器接入同一网络。校验通过后才启动
正式 Nginx。`ps` 和 `logs` 用于确认最终容器健康状态及启动错误。

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
docker compose $COMPOSE_FILES exec app sh -c 'echo workers=$UVICORN_WORKERS http=$HTTP_MAX_CONNECTIONS downloads=$GENERATED_IMAGE_DOWNLOAD_CONCURRENCY db=$DB_POOL_SIZE'
```

2C2G 预期：app 上限约 1.6 CPU、1400 MiB；nginx 上限约 0.4 CPU、300 MiB；宿主机仍保留资源给 Docker、文件缓存和 SSH。

## 9. 日常更新、监控与备份

每次 SSH 登录都是新的 Shell，`COMPOSE_FILES` 不会跨会话保留。下面每个可独立复制的运维代码块都会先重新 `export COMPOSE_FILES`，后续 Compose 命令保持简短且含义一致。

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

# 迁移期间停止入口和旧 app，避免 SQLite 继续写入
docker compose $COMPOSE_FILES stop nginx app
docker compose $COMPOSE_FILES run --rm --no-deps app \
  alembic upgrade head

# 与首次部署相同：先启动 app，再校验并启动 Nginx
docker compose $COMPOSE_FILES up -d --force-recreate app
docker compose $COMPOSE_FILES run --rm nginx nginx -t
docker compose $COMPOSE_FILES up -d --force-recreate --remove-orphans nginx
docker compose $COMPOSE_FILES ps
docker compose $COMPOSE_FILES logs --tail=100 app nginx
curl -fsS "https://$DOMAIN/health/ready"
```

这里显式写 `origin main`，避免本地分支没有 upstream、错误跟踪功能分支或远程默认分支变化时，裸 `git pull --ff-only` 拉错目标。更新流程先完成配置检查、镜像拉取和构建，再停止入口与旧 app；因此构建失败不会造成停机。停止服务后迁移 SQLite，避免旧 app 并发写库；随后严格按“app → Nginx 校验 → 正式 Nginx”的顺序恢复服务，避免再次出现 upstream 域名无法解析的问题。

常用排障：

```bash
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
cd /opt/st-imagen
export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'
git checkout <上一个已验证 commit>
docker compose $COMPOSE_FILES up -d --build
```

迁移失败：把 Cloudflare A 记录切回旧服务器 IP，并用相同的两个 Compose 文件重新启动旧机。不要让新旧两台同时接受写入后再合并 SQLite。

## 12. 集中故障排查

若这是新 SSH 会话，先执行 `cd /opt/st-imagen` 和
`export COMPOSE_FILES='-f compose.prod.yml -f compose.cloudflare.yml'`，再运行本节中的 Compose 命令。

正常部署只按 1～11 节顺序执行。本节只在出现对应错误时使用，不要把修复命令混入首次部署。

### 12.1 SSH 握手成功但 root 密码失败

新建 VPS 应使用第 1 节 SSH key 流程。旧密码机出现 `Authentication failed (password)` 时，
22 端口和 sshd 已经可达；通过云厂商控制台的“重置密码”、VNC/串口或救援模式恢复。恢复后检查：

```bash
passwd -S root
chage -l root
sshd -T -C user=root,host="$(hostname)",addr="你的当前公网IP" \
  | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
```

只有 root 密码时不要跳过 2.3 和 2.4 后又执行禁用 root/password 的命令。紧急临时文件可用
`00-emergency-recovery.conf` 设置 `PermitRootLogin yes`、`PasswordAuthentication yes`，但
验证 deploy key 后必须删除。若存在陌生登录/公钥或密码自行变化，把云实例视为已失陷并
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
创建云实例时绑定公钥所对应的私钥，而不是另一个同名或旧密钥。

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
IMAGE=$(sudo find data/uploads/generated -maxdepth 1 -type f -printf '%f\n' | head -n 1)
docker compose $COMPOSE_FILES exec --user 101 nginx test -r "/srv/uploads/generated/$IMAGE"
```

不要对整个 `data` 执行 `chmod -R 755`；数据库仍需保护。权限立即生效，无需重启容器。

### 12.5 登录正常但生图 502 非 JSON

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

### 12.6 nginx 证书不可读

`cannot load certificate ... Permission denied`：

```bash
sudo chown root:root deploy/certs/origin.pem deploy/certs/origin.key
sudo chmod 644 deploy/certs/origin.pem
sudo chmod 600 deploy/certs/origin.key
docker compose $COMPOSE_FILES run --rm --no-deps nginx nginx -t
```

### 12.7 nginx 临时目录不能 chown

`chown("/var/cache/nginx/client_temp", 101) failed` 表示旧版 Compose 缺 capability。更新代码，
确认 `compose.prod.yml` 的 `cap_add` 包含 `CHOWN`、`NET_BIND_SERVICE`、`SETGID`、`SETUID`，然后：

```bash
git fetch origin --prune
git switch main
git pull --ff-only origin main
docker compose $COMPOSE_FILES up -d --force-recreate nginx
```

不要使用 `chmod 777` 或 `privileged: true`。

## 官方参考

- 阿里云 ECS 实例创建：<https://help.aliyun.com/zh/ecs/user-guide/create-an-instance-on-the-custom-launch-tab>
- 阿里云安全组：<https://help.aliyun.com/zh/ecs/user-guide/overview-44>
- Docker Engine on Ubuntu：<https://docs.docker.com/engine/install/ubuntu/>
- Docker Linux post-install：<https://docs.docker.com/engine/install/linux-postinstall/>
- Cloudflare 代理状态：<https://developers.cloudflare.com/dns/proxy-status/>
- Cloudflare Full (strict)：<https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/>
- Cloudflare Origin CA：<https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
- Cloudflare IP ranges：<https://www.cloudflare.com/ips/>
