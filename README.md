# ST Image Gen

一个最小可行的 ST 图像生成代理：

- **前端**：文生图（Nano Banana Pro、GPT Image 2）/ 图生图（Nano Banana Pro、gpt-image-1.5），SSE 流式进度。
- **后台**：多账号管理（增删改查、启停）、生成日志（点击「查看」当前页弹出预览）。
- **存储**：上游生成完成后，图片先流式下载到服务器临时文件并原子改名；本地文件落盘完成后才返回本站图片 URL，生成日志随后异步写入数据库。
- **架构**：浏览器 → 本服务（FastAPI，SSE 转发）→ ST 工作流 API。

> 账号级每日额度不是当前功能。账号调度只看账号状态、进程内并发上限与故障隔离状态；用户级每日额度仍由用户权限配置控制。

用户额度按“工作流实际执行”计费：准入失败、没有进入节点执行、连接失败以及工作流长时间无进度/总超时均不扣额度；一旦上游报告节点开始执行、返回节点级错误（例如 `Error in Node ...`）或产出图片，即计为一次使用。该规则同时适用于账号密码用户和邀请码访客。



---

## JS vs Python 调用方式

| 维度 | 前端直接调 ST（纯 JS） | 后端代理（Python + 前端 JS）|
|---|---|---|
| 安全 | ❌ Token 暴露在浏览器 | ✅ Token 只在服务端 |
| 多账号轮询 / 失败切换 | ❌ 无法在客户端做 | ✅ 后端选号 |
| 配额、日志、审计 | ❌ 无法做 | ✅ 全部可记录 |
| CORS | ⚠️ 受 ST 域策略限制 | ✅ 自有域 |
| 隐藏 org_id / flow_id | ❌ 暴露 | ✅ 隐藏 |

**结论**：本项目采用 **后端 Python（FastAPI + httpx 异步）** 调用 ST，前端用 JS 调本服务。

---

## 目录结构

```
st-imagen/
├── app/
│   ├── main.py                 # FastAPI 主入口
│   ├── models/database.py      # SQLAlchemy 模型 + SQLite
│   ├── routers/
│   │   ├── admin.py            # 管理 API（登录 / 账号 CRUD / 日志）
│   │   ├── generate.py         # 生图 API
│   │   └── user_auth.py        # 用户登录、邀请码与会话
│   ├── services/
│   │   ├── auth.py             # JWT + bcrypt 管理员认证
│   │   ├── crypto.py           # Fernet 加密 api_key
│   │   ├── st_client.py        # ST 异步 HTTP 客户端
│   │   ├── account_pool.py     # 账号池、并发槽位与故障隔离
│   │   ├── guard.py            # 全局并发、限流与熔断
│   │   ├── app_settings.py     # 管理后台运行时设置
│   │   ├── user_auth.py        # 用户、邀请码与用户额度
│   │   ├── deps.py             # 通用认证依赖
│   │   ├── outbound_url.py     # 出站 URL 安全校验
│   │   └── upstream_redaction.py # 上游错误脱敏
│   └── static/                 # 前端静态文件（无构建步骤）
│       ├── index.html / app.js
│       └── admin.html / admin.js
│       └── style.css
├── deploy/nginx.conf            # 生产反向代理与 SSE 配置
├── scripts/                     # 压测与压测数据清理工具
├── tests/                       # 单元测试与韧性测试
├── compose.prod.yml             # 生产 Docker Compose
├── compose.vps-stress.yml       # 2c2g 容器压测 Compose
├── Dockerfile
├── data/                        # SQLite、上传和生成图片持久化目录
├── requirements.txt
├── .env.example
└── run.py
```

---

## 上手 5 步

### 开发检查

提交改动前，可运行后端测试与前端端到端测试：

```bash
pytest
npm run test:e2e
```

### 1. 安装依赖

```bash
cd st-imagen
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

务必修改：

- `ENCRYPTION_KEY`：用于加密存储账号 api_key。生成方式：
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `JWT_SECRET_KEY`：随机字符串即可。
- `ST_BASE_URL`：ST API 根地址；示例值仅为占位符，部署时必须替换。
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`：首次启动会创建默认管理员，请改成你自己的。
- `ADMIN_PATH`：管理后台路径，默认 `admin`，建议改成你自己的不公开路径，例如 `ops-2026-console`。

### 3. 启动

```bash
python run.py
```

注意：当 `.env` 里 `DEBUG=false` 时，Python 后端代码修改后必须重启进程；只有 `app/static/*` 这类静态文件改动会在刷新页面后立即看到效果。

控制台会打印类似：

```
============================================================
 ST Image Gen v0.1.0
 Frontend  : /
 Admin page: /admin
============================================================
```

如果你把 `ADMIN_PATH` 改成了自定义值，启动后这里会打印真实后台入口。
首页不再显示「控制台」入口，后台请直接访问：

```text
http://localhost:8001/<你的 ADMIN_PATH>
```

### 4. 在管理后台添加账号

打开 `http://localhost:8001/<你的 ADMIN_PATH>`，登录后点 **+ 新增账号**：

- **名称**：建议直接填 ST 登录邮箱，例如 `iskerguo@gmail.com`（管理后台显示完整名称，前台/日志列只显示 `@` 前的部分）。
- **org_id**：ST 工作流 URL 中的第一段，例如 `3b0c67e9-89d4-42ba-bc69-544e3cf8bd41`。
- **flow_id**：URL 中的第二段，例如 `691af0876d6b6da025de1ab2`。
- **API Key**：ST 给的 Bearer Token（`sk-...`，粘贴时无需加 `Bearer `）。这是 inference 用的 Public Key。
- **Private API Key**（可选）：ST 控制台 → API Keys 里另外创建一个 **Private** 类型的 Key，仅用于失败时调用 `/analytics` 拉取运行详情里的 `Errors` 字段（节点真实报错）。**不填**也能跑，失败时只看到通用兜底文案。

> 同一套工作流模板：所有账号共享 in-0~in-6 输入约定。账号调度不使用账号级每日额度；账号主要配置工作流、密钥、状态和并发上限。用户级每日额度由用户/邀请码配置控制。

### 5. 生图

回到 `http://localhost:8001/`，输入提示词、选择模型和参数，点 **立即生成**。前端走 SSE，只接收生成进度计数、结果状态和本站图片地址；上游原始事件、运行标识与账号信息不会发送到浏览器。

- **登录**：已有账号可使用用户名和密码登录；持有邀请码时选择「邀请码进入」，只输入邀请码即可创建访客会话并生图，无需注册或保存用户名和密码。
- **文生图**：默认模式，可选择 Nano Banana Pro（画幅、清晰度）或 GPT Image 2（Size、Quality）。GPT Image 2 的 Size 默认为 `1024x1024`。
- **图生图**：切换到「图生图」Tab，模型 = Nano Banana Pro 或 `gpt-image-1.5`；可以上传本地图片，也可以添加公网图片直链，最多 5 张。服务器上传的参考图必须通过 `PUBLIC_BASE_URL` 对 ST 公网可达。

## 生产部署（Docker Compose）

> DigitalOcean 4C8G 临时部署、Cloudflare Origin CA、完整安全加固、验收、备份以及之后迁移到阿里云 2C2G 的逐步操作，见 [DigitalOcean + Cloudflare 部署手册](docs/deploy-digitalocean-cloudflare.md)。

生产编排按单机 `2c2g` 设计：FastAPI 固定单 worker，由 nginx 提供静态文件和 SSE 反向代理。首次部署准备干净的数据目录即可，SQLite 数据库和图片会在容器启动后自动创建。

```bash
cd /path/to/st-imagen
cp .env.example .env
# 修改 .env 中的 ENCRYPTION_KEY、JWT_SECRET_KEY、ADMIN_PASSWORD、PUBLIC_BASE_URL 等生产配置
mkdir -p data/uploads/generated
sudo chown -R 10001:10001 data
sudo chmod 750 data
sudo find data/uploads -type d -exec chmod 755 {} +
sudo find data/uploads -type f -exec chmod 644 {} +
docker compose -f compose.prod.yml config --quiet
docker compose -f compose.prod.yml up -d --build
docker compose -f compose.prod.yml ps
```

`data/` 是唯一需要持久化的目录，包含 SQLite 数据库和上传/生成图片；压测报告、临时图片、Python 缓存和本地虚拟环境不属于部署内容。生产环境不要使用 `compose.vps-stress.yml`，该文件只用于受限容器压测。当前 Compose 暴露 HTTP `80` 端口，HTTPS 证书应由云 LB 或外层反向代理负责；启用 `USER_SESSION_SECURE=true` 时，生产访问必须经过 HTTPS。

### 数据库迁移与备份

新部署和版本升级使用 Alembic 管理数据库版本：

```bash
alembic upgrade head
```

已有数据库首次接入迁移体系时，先备份并确认当前应用可正常启动，再执行
`alembic stamp 20260827_0001`。生产升级前可创建 SQLite 一致性备份：

```bash
python scripts/backup_data.py --include-uploads
```

容器以 UID/GID `10001` 的非 root 用户运行，因此宿主机 `data/` 必须授予该用户写权限。
存活检查为 `/health/live`，就绪检查为 `/health/ready`；后者同时检查数据库和磁盘余量。

## 2c2g VPS 仿真压测

项目里原本已经有：

- `scripts/stress_concurrent.py`：直接打 `/api/generate/stream`，适合看纯接口并发上限。
- `scripts/stress_real_users.py`：模拟登录用户流程，适合看更接近前台真实使用的并发。

现在额外提供了一套 **2 CPU / 2 GiB 内存** 的容器化仿真入口：

```bash
cd /path/to/st-imagen
python3 scripts/vps_stress.py up --build
```

它会用 [compose.vps-stress.yml](compose.vps-stress.yml) 启一个受限容器。压测前需要准备 `.env`，并确保 Docker Compose 可以访问 ST 上游：

- CPU：`2`
- 内存：`2g`
- Uvicorn worker：默认 `1`（进程内并发闸门要求单 worker）
- 端口：宿主机 `18001` -> 容器 `8001`

### 真实用户阶梯压测

```bash
python3 scripts/vps_stress.py run-real --ensure-up --build --stages 10,20,30
```

默认行为：

- 每阶段复现 `/api/auth/status -> /api/auth/me -> /api/options -> /api/recent-images -> /api/generate/stream`
- 生成请求默认带 `1500ms` 轻微错峰，避免所有用户完全同一毫秒起跑
- 阶段之间默认暂停 `10s`
- 同时采样 `docker stats`，输出到 `data/stress_reports/docker_stats_*.jsonl`
- 结束后会额外写一份 `vps_bundle_*.json`，把压测报告和容器资源摘要串起来

### 直接接口并发压测

```bash
python3 scripts/vps_stress.py run-concurrent --ensure-up --build --concurrency 20
```

这个模式更适合先摸清单轮 SSE 并发上限，再决定真实用户模式的阶梯区间。

### 建议的 2c2g 试压顺序

1. 先跑 `run-concurrent --concurrency 10`、`20`、`30`，看成功率、`first_event_ms`、`total_ms` 是否明显恶化。
2. 再跑 `run-real --stages 10,20,30`，观察在真实登录态和最近图片读取存在时是否更早出现失败。
3. 重点看 `vps_bundle_*.json` 里的 CPU / 内存摘要，以及原始 `docker_stats_*.jsonl`。
4. 如果 CPU 长时间接近 `200%`、内存逼近 `2g`、同时成功率下滑，就把前一档并发视为更稳妥的 VPS 上限。

### 为什么要这样测

这个服务的主要压力不是本地 CPU 算图，而是：

- SSE 长连接数量
- 对上游 ST 的并发等待
- SQLite 在高并发下的串行写入竞争
- 生成结果下载与落盘

所以单纯在开发机上无限资源跑并发，不足以代表 `2c2g VPS` 的真实表现；把服务本体先限制在 `2c2g` 容器里，再从宿主机发压，更接近部署后的实况。

---

## API 速览

### 公开接口

- `GET /health` —— Docker/反向代理使用的健康检查，返回服务版本和 `status=ok`。
- `GET /api/options` —— 返回 `text2img` / `img2img` 两组下拉选项，结构：
  ```json
  {
    "text2img": {
      "models": [
        {"label": "Nano Banana Pro", "value": "Nano Banana Pro"},
        {"label": "GPT Image 2", "value": "GPT Image 2"}
      ],
      "aspect_ratios": ["1:1", ...],
      "resolutions": ["1K", "2K", "4K"],
      "sizes": ["1024x1024", "1536x1024", "1024x1536", "2048x2048", "3840x2160"],
      "qualities": ["auto", "low", "medium", "high"]
    },
    "img2img": {
      "models": [
        {"label": "Nano Banana Pro", "value": "gemini-3-pro-image-preview"},
        {"label": "gpt-image-1.5", "value": "gpt-image-1.5"}
      ]
    }
  }
  ```
- `GET /api/generate/capacity` —— 返回当前全局/账号容量提示；该接口不保证提交时仍有空闲槽位。
- `POST /api/generate` —— 同步生图（上游图片先下载到本站，落盘成功后一次性返回本站图片 URL）。
- `POST /api/generate/stream` —— **SSE 流式生图**。请求体同上。响应是若干 `data: {json}\n\n` 帧，事件类型：
  - `start` —— 已选号、即将调用上游：`{type, account_id, account_name, account_short, mode, model}`
  - `upstream` —— 上游进度摘要：`{type, line}`；其中所有 HTTP(S) URL 都会被隐藏，不转发上游原文链接。
  - `complete` —— 完成：`{type, images, response_time_ms, account_id, account_name, account_short}`；`images` 只包含本站 `/uploads/generated/...` 地址，图片本地落盘完成后才发送该事件。
  - `error` —— 失败：`{type, status_code, message, upstream, elapsed_ms}`
- `POST /api/reference-image` —— 上传参考图到本服务，返回 `/uploads/...` 公网/站内可访问地址（需登录）。上传地址最终必须能被 ST 访问。
- `POST /api/reference-url/validate` —— 预检参考图直链是否可访问、是否为图片，并阻止内网/保留地址探测（需登录）。

> 建议在 VPS / 反向代理环境配置 `PUBLIC_BASE_URL=https://你的域名/`，这样接口返回的图片链接会直接是公网域名，而不是内网地址。

### 并发与过载策略

本服务部署在外部 ST 推理 API 前面，生产使用单 Uvicorn worker。生成请求不在服务端排队：

- 全局、用户和账号容量在进程内原子准入；有空闲槽位才会返回 SSE 200。
- 满载直接返回真正的 HTTP 429，并带 `Retry-After`；不会返回 200 后再发送 SSE 429。
- 前端容量接口 `/api/generate/capacity` 只用于提示，提交时的服务端准入才是最终结果。
- 上游生成完成后立即释放推理槽，图片本地下载/落盘不占用生成容量；响应会等待本地文件落盘完成，绝不回退或暴露上游图片 URL。历史数据库写入在落盘后异步完成。
- 浏览器不自动重试 429，避免高峰时形成重试风暴。

建议的 2c2g 默认值：`GENERATION_GLOBAL_MAX_CONCURRENT=90`、`ACCOUNT_MAX_INFLIGHT=10`、`HTTP_MAX_CONNECTIONS=128`、`GENERATED_IMAGE_DOWNLOAD_CONCURRENCY=32`、`DB_POOL_SIZE=4`、`DB_MAX_OVERFLOW=0`。容量不足时请求直接返回 429，不在服务端等待。90 个请求能否全部进入上游，还取决于启用账号的并发容量总和；账号池不足时仍会按设计返回 429。

账号并发槽位只保存在单 worker 进程内，使用一次性 token 保证重复清理不会重复释放；当前没有持久化账号租约，也没有账号等待队列。账号“故障隔离”是另一项保护：仅在密钥、账号配置或明确的账号级上游错误时临时停用该账号，隔离时间由 `ACCOUNT_FAILURE_ISOLATION_SECONDS` 控制，不参与正常租约管理。旧数据库中的 `account_leases` 历史表及 `accounts` 表历史调度列不会被主动删除，但运行时不再读取或写入。

### 生图超时分层

工作流生图的总预算为 `230s`，各层超时分别负责不同阶段，不能简单全部设成同一个值：

- 所有工作流连续无进度 `200s` 后失败。
- 工作流总耗时上限为 `230s`，在 200 秒无进度预算外保留错误收尾和资源释放余量。
- ST 传输保护为 `270s`，给工作流结束和错误收尾留出余量；单次 SSE 读取保护为 `330s`。
- 浏览器无任何 SSE 数据 `220s` 才超时；服务端会每 `15s` 发送 keepalive，因此它不会限制正常的 230 秒工作流。
- 生成完成后的图片下载是独立阶段，仍使用下载超时和下载总预算，不占用工作流生成预算。

### 管理接口（需 `Authorization: Bearer <jwt>`)

- `POST /api/admin/login`
- `GET  /api/admin/me`
- `POST /api/admin/change-password`
- `GET  /api/admin/accounts`
- `POST /api/admin/accounts`（账号只配置工作流、密钥、状态和并发上限）
- `POST /api/admin/accounts/import` —— 批量导入账号
- `PUT  /api/admin/accounts/{id}`
- `POST /api/admin/accounts/bulk/status` —— 批量启用/停用账号
- `POST /api/admin/accounts/{id}/test` —— 用最小输入触发一次同步调用，验证账号可达性
- `POST /api/admin/accounts/isolation/clear` —— 清除全部账号故障隔离
- `POST /api/admin/accounts/{id}/isolation/clear` —— 清除指定账号故障隔离
- `DELETE /api/admin/accounts/{id}`
- `GET  /api/admin/invite-codes`、`POST /api/admin/invite-codes`
- `GET  /api/admin/users`、`POST /api/admin/users`、`POST /api/admin/users/batch`
- `GET  /api/admin/stats/overview`
- `GET  /api/admin/runtime-metrics` —— 轻量实时快照：全局/账号在途并发和各模型并发，不访问数据库
- `GET  /api/admin/runtime-status` —— 低频运行诊断：熔断器、路由、账号隔离和图片落盘状态；无隔离账号时完全不访问数据库，隔离账号名称仅在需要时查询
- `GET  /api/admin/settings` —— 返回图片保留设置，以及只读的并发、连接池、单 worker 配置
- `PUT  /api/admin/settings`、`POST /api/admin/settings/cleanup` —— 更新图片保留设置、清理日志或图片
- `POST /api/admin/circuit-breaker/reset`、`POST /api/admin/circuit-breaker/routes/reset`
- `GET  /api/admin/logs?limit=50` —— 返回字段含 `account_name` 与 `is_stream`

---

## 工作流模板约定

所有账号共用一套 ST 工作流，输入约定（前端隐藏，后端拼接）：

| 字段 | 含义 | 示例 |
|---|---|---|
| `in-0` | 提示词 | `达芬奇风格的解剖君主斑蝶...` |
| `in-1` | Nano Banana Pro 画幅 | `1:1`（图生图前端隐藏，后端兜底） |
| `in-2` | Nano Banana Pro 清晰度 | `2K`（图生图前端隐藏，后端兜底） |
| `in-3` | 模型 | 文生图：`Nano Banana Pro` 或 `GPT Image 2`；图生图：`gemini-3-pro-image-preview` 或 `gpt-image-1.5` |
| `in-4` | GPT Image 2 Size | `1024x1024`、`1536x1024`、`1024x1536`、`2048x2048`、`3840x2160` |
| `in-5` | GPT Image 2 Quality | `auto`、`low`、`medium`、`high` |
| `in-6` | 参考图 URL（多个时按换行拼接） | `https://.../ref.jpg`（文生图传空串） |

配置覆盖：

- `UI_MODELS=...,...`（文生图模型，逗号分隔；若设置该项且需要 GPT Image 2，请显式包含 `GPT Image 2`）
- `UI_IMG2IMG_MODELS=Nano Banana Pro=gemini-3-pro-image-preview,gpt-image-1.5=gpt-image-1.5`（图生图，`label=value` 形式）
- `UI_ASPECT_RATIOS` / `UI_RESOLUTIONS`

---

## 选号策略（MVP 版）

- 仅在 `status='active'` 的账号中挑选；
- 优先 `in_flight` 低的账号，其次 `last_used_at` 更早、`created_at` 更早的账号；
- **同步接口** `POST /api/generate`：明确的账号/凭据类上游错误会尝试一个备用账号；公共上游路由故障不会向整个账号池扩散，参数类 4xx 直接回传；
- **流式接口** `POST /api/generate/stream`：当前实现仅选一个账号，发生错误以 `event=error` 事件结束流（流式过程中无法再切号）。

---

## 后续可扩展点（TODO）

- **流式接口的失败切号**：在没有 yield 任何 `upstream` 事件之前可以重试到下一个账号；目前简化为 1 次。
- **跨进程多 worker 部署**：当前限流、并发和账号槽位依赖单 worker 进程内状态；后续可改为共享状态后再扩展多 worker。
- **Prometheus 指标**：当前主要通过管理后台 runtime-status 和日志观察运行状态。
- **用户级 API Key**：前台分级使用。

可参考同仓库 `st-api` 的 `account_pool.py` / `backend_client.py` 等做更完整的扩展。
