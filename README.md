# StackAI Image Gen

一个最小可行的 StackAI 图像生成代理：

- **前端**：文生图（Nano Banana Pro、GPT Image 2）/ 图生图（Nano Banana Pro、gpt-image-1.5），SSE 流式进度。
- **后台**：多账号管理（增删改查、启停）、生成日志（点击「查看」当前页弹出预览）。
- **存储**：生成结果会自动下载到项目内 `data/uploads/generated/`，接口返回本站 `/uploads/generated/...` 公开链接。
- **架构**：浏览器 → 本服务（FastAPI，SSE 转发）→ StackAI 工作流 API。

> 管理后台仍保留账号 `daily_quota` 字段，但当前生图调度**不再按账号配额限流**；实际调度仅看账号状态、并发上限与冷却状态。

> StackAI 的 Bearer Token **绝不暴露给浏览器**。前端只调用本服务的 `/api/generate`，由后端选号并代理上游。

---

## JS vs Python 调用方式

| 维度 | 前端直接调 StackAI（纯 JS） | 后端代理（Python + 前端 JS）|
|---|---|---|
| 安全 | ❌ Token 暴露在浏览器 | ✅ Token 只在服务端 |
| 多账号轮询 / 失败切换 | ❌ 无法在客户端做 | ✅ 后端选号 |
| 配额、日志、审计 | ❌ 无法做 | ✅ 全部可记录 |
| CORS | ⚠️ 受 StackAI 域策略限制 | ✅ 自有域 |
| 隐藏 org_id / flow_id | ❌ 暴露 | ✅ 隐藏 |

**结论**：本项目采用 **后端 Python（FastAPI + httpx 异步）** 调用 StackAI，前端用 JS 调本服务。

---

## 目录结构

```
stackai-image-gen/
├── app/
│   ├── main.py                 # FastAPI 主入口
│   ├── models/database.py      # SQLAlchemy 模型 + SQLite
│   ├── routers/
│   │   ├── admin.py            # 管理 API（登录 / 账号 CRUD / 日志）
│   │   └── generate.py         # 生图 API
│   ├── services/
│   │   ├── auth.py             # JWT + bcrypt 管理员认证
│   │   ├── crypto.py           # Fernet 加密 api_key
│   │   ├── stackai_client.py   # StackAI 异步 HTTP 客户端
│   │   ├── account_pool.py     # 账号池 + 简单选号策略
│   │   └── deps.py             # 通用依赖（require_admin）
│   └── static/                 # 前端静态文件（无构建步骤）
│       ├── index.html / app.js
│       └── admin.html / admin.js
│       └── style.css
├── data/                        # SQLite 持久化目录
├── requirements.txt
├── .env.example
└── run.py
```

---

## 上手 5 步

### 1. 安装依赖

```bash
cd stackai-image-gen
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
 StackAI Image Gen v0.1.0
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

- **名称**：建议直接填 StackAI 登录邮箱，例如 `iskerguo@gmail.com`（管理后台显示完整名称，前台/日志列只显示 `@` 前的部分）。
- **org_id**：StackAI 工作流 URL 中的第一段，例如 `3b0c67e9-89d4-42ba-bc69-544e3cf8bd41`。
- **flow_id**：URL 中的第二段，例如 `691af0876d6b6da025de1ab2`。
- **API Key**：StackAI 给的 Bearer Token（`sk-...`，粘贴时无需加 `Bearer `）。这是 inference 用的 Public Key。
- **Private API Key**（可选）：StackAI 控制台 → API Keys 里另外创建一个 **Private** 类型的 Key，仅用于失败时调用 `/analytics` 拉取运行详情里的 `Errors` 字段（节点真实报错）。**不填**也能跑，失败时只看到通用兜底文案。

> 同一套工作流模板：所有账号共享 in-0~in-6 输入约定。每日配额由服务端统一锁死为 1M，无需在表单里填。

### 5. 生图

回到 `http://localhost:8001/`，输入提示词、选择模型和参数，点 **立即生成**。前端走 SSE，会实时显示工作流当前节点（prompt / model / image_size / ...）。

- **登录**：已有账号可使用用户名和密码登录；持有邀请码时选择「邀请码进入」，只输入邀请码即可创建访客会话并生图，无需注册或保存用户名和密码。
- **文生图**：默认模式，可选择 Nano Banana Pro（画幅、清晰度）或 GPT Image 2（Size、Quality）。GPT Image 2 的 Size 默认为 `1024x1024`。
- **图生图**：切换到「图生图」Tab，模型 = Nano Banana Pro 或 `gpt-image-1.5`；参考图必须是公网可访问 URL，可在 URL 输入框以逗号分隔多张图片后按回车加入。

## 2c2g VPS 仿真压测

项目里原本已经有：

- `scripts/stress_concurrent.py`：直接打 `/api/generate/stream`，适合看纯接口并发上限。
- `scripts/stress_real_users.py`：模拟登录用户流程，适合看更接近前台真实使用的并发。

现在额外提供了一套 **2 CPU / 2 GiB 内存** 的容器化仿真入口：

```bash
cd /home/ww/Project/st-imagen
python3 scripts/vps_stress.py up --build
```

它会用 [compose.vps-stress.yml](/home/ww/Project/st-imagen/compose.vps-stress.yml) 启一个受限容器：

- CPU：`2`
- 内存：`2g`
- Uvicorn worker：默认 `2`
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
- 对上游 StackAI 的并发等待
- SQLite 在高并发下的串行写入竞争
- 生成结果下载与落盘

所以单纯在开发机上无限资源跑并发，不足以代表 `2c2g VPS` 的真实表现；把服务本体先限制在 `2c2g` 容器里，再从宿主机发压，更接近部署后的实况。

---

## API 速览

### 公开接口

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
- `POST /api/generate` —— 同步生图（一次性返回图片 URL，仍保留供调试）。
- `POST /api/generate/stream` —— **SSE 流式生图**。请求体同上。响应是若干 `data: {json}\n\n` 帧，事件类型：
  - `start` —— 已选号、即将调用上游：`{type, account_id, account_name, account_short, mode, model}`
  - `upstream` —— 上游每条进度行：`{type, line}`，`line` 是 StackAI 原文 JSON 字符串
  - `complete` —— 完成：`{type, images, raw, response_time_ms, account_id, account_name, account_short}`；其中 `images` 已替换为项目本地保存后的公开链接。
  - `error` —— 失败：`{type, status_code, message, upstream, elapsed_ms}`
- `POST /api/reference-image` —— 上传参考图到本服务，返回 `/uploads/...` 公网/站内可访问地址（需登录）。
- `POST /api/reference-url/validate` —— 预检参考图直链是否可访问、是否为图片，并阻止内网/保留地址探测（需登录）。

> 建议在 VPS / 反向代理环境配置 `PUBLIC_BASE_URL=https://你的域名/`，这样接口返回的图片链接会直接是公网域名，而不是内网地址。

### 生图超时分层

工作流生图的总预算为 `200s`，各层超时分别负责不同阶段，不能简单全部设成同一个值：

- 普通工作流连续无进度 `90s` 后失败；GPT Image 2 和 Nano Banana Pro 4K 放宽到 `150s`。
- 工作流总耗时上限为 `200s`。
- StackAI 传输保护为 `240s`，给工作流结束和错误收尾留出余量；单次 SSE 读取保护为 `300s`。
- 浏览器无任何 SSE 数据 `170s` 才超时；服务端会每 `15s` 发送 keepalive，因此它不会限制正常的 200 秒工作流。
- 生成完成后的图片下载是独立阶段，仍使用下载超时和下载总预算，不占用工作流生成预算。

### 管理接口（需 `Authorization: Bearer <jwt>`)

- `POST /api/admin/login`
- `GET  /api/admin/accounts`
- `POST /api/admin/accounts`（`daily_quota` 字段会被忽略，统一锁死 1M）
- `PUT  /api/admin/accounts/{id}`
- `POST /api/admin/accounts/{id}/test` —— 用最小输入触发一次同步调用，验证账号可达性
- `DELETE /api/admin/accounts/{id}`
- `GET  /api/admin/stats/overview`
- `GET  /api/admin/logs?limit=50` —— 返回字段含 `account_name` 与 `is_stream`

---

## 工作流模板约定

所有账号共用一套 StackAI 工作流，输入约定（前端隐藏，后端拼接）：

| 字段 | 含义 | 示例 |
|---|---|---|
| `in-0` | 提示词 | `达芬奇风格的解剖君主斑蝶...` |
| `in-1` | Nano Banana Pro 画幅 | `1:1`（图生图前端隐藏，后端兜底） |
| `in-2` | Nano Banana Pro 清晰度 | `2K`（图生图前端隐藏，后端兜底） |
| `in-3` | 模型 | 文生图：`Nano Banana Pro` 或 `GPT Image 2`；图生图：`gemini-3-pro-image-preview` 或 `gpt-image-1.5` |
| `in-4` | GPT Image 2 Size | `1024x1024`、`1536x1024`、`1024x1536`、`2048x2048`、`3840x2160` |
| `in-5` | GPT Image 2 Quality | `auto`、`low`、`medium`、`high` |
| `in-6` | 参考图 URL | `https://.../ref.jpg`（文生图传空串） |

配置覆盖：

- `UI_MODELS=...,...`（文生图模型，逗号分隔；若设置该项且需要 GPT Image 2，请显式包含 `GPT Image 2`）
- `UI_IMG2IMG_MODELS=Nano Banana Pro=gemini-3-pro-image-preview,gpt-image-1.5=gpt-image-1.5`（图生图，`label=value` 形式）
- `UI_ASPECT_RATIOS` / `UI_RESOLUTIONS`

---

## 选号策略（MVP 版）

- 仅在 `status='active'` 的账号中挑选；
- 优先 `in_flight` 低的账号，其次 `last_used_at` 更早、`created_at` 更早的账号；
- **同步接口** `POST /api/generate`：触发上游 5xx / 429 时自动失败切换到下一个账号（最多 `max_failover` 次），4xx（除 429）视为请求错误直接回传，不切号；
- **流式接口** `POST /api/generate/stream`：当前实现仅选一个账号，发生错误以 `event=error` 事件结束流（流式过程中无法再切号）。

---

## 后续可扩展点（TODO）

- **图片上传**：当前图生图必须给一个公网可访问 URL。要支持本地上传，需要服务部署在公网（或反代映射）—— 工作流的 `in-6` 字段最终被上游工作流直接拉取。可选实现路线：
  1. 部署到公网域名，加一个 `POST /api/uploads` 路由把文件落到 `data/uploads/`，回传公开 URL；
  2. 接入第三方图床（imgbb / Cloudinary / S3）；
  3. 试探 `in-4` 是否接受 `data:image/png;base64,...`（未验证，多数模型不支持）。
- **流式接口的失败切号**：在没有 yield 任何 `upstream` 事件之前可以重试到下一个账号；目前简化为 1 次。
- **限流重试的随机抖动**：前端收到短时 429/502/503/504 以及 `retry_after` 后，会在建议等待时间上随机增加 0～3 秒，再允许下一次手动生成，避免并发客户端同刻重试；长期配额冷却不加抖动，也不会自动重复提交生成请求。
- **多 worker 部署、日志清理、Prometheus 指标**。
- **用户级 API Key**：前台分级使用。

可参考同仓库 `st-api` 的 `account_pool.py` / `backend_client.py` 等做更完整的扩展。
