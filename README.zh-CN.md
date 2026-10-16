<div align="center">

<img src="logo.svg" alt="Immich LLM Tagger Logo" width="128" height="128" style="aspect-ratio: 1/1; object-fit: contain; border-radius: 20px;" />

# Immich LLM Tagger

**基于多模态大语言模型（Vision LLM）的 Immich 照片自动分类、智能打标、相册归集与时间线归档自托管服务。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](pyproject.toml)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](docker-compose.yml)
[![FastAPI](https://img.shields.io/badge/FastAPI-Framework-green.svg)](https://fastapi.tiangolo.com)

**[English](README.md)** | **[简体中文](README.zh-CN.md)**

</div>

---

## 📖 项目简介

在使用手机同步照片到 Immich（如 iCloud 同步、Google Photos 同步或 Immich 官方 App 自动备份）时，个人相册主时间线经常充斥着大量临时杂图：**屏幕截图、发票票据、证件凭证、表情包、广告海报和误触废片**。

**Immich LLM Tagger** 专为解决此痛点而设计。它利用轻量多模态视觉模型（如 DeepSeek-VL、Qwen2.5-VL、MiniCPM、GPT-4o 或本地 Ollama 视觉大模型），为 Immich 提供全自动的整理服务：

1. **自动归档杂图**：将屏幕截图、聊天记录、表情包、广告图移出主时间线（放入 Immich 归档/Archive）。
2. **智能归集相册**：将纸质文档发票自动归入 `文档资料` 相册，证件卡片归入 `证件凭据` 相册。
3. **隐私敏感标记**：自动识别身份证件、银行卡、密码私密信息并打上 `敏感隐私` 标记。
4. **保留生活相片**：真实生活摄影（家人、旅行、美食、宠物）完整保留在主时间线上。
5. **完全可逆与安全**：所有操作均落盘详细审计日志，支持任务级一键回滚（Undo）和安全重置。

---

## ✨ 核心特性

- 🧠 **广泛兼容 Vision LLM**：完美支持任何 OpenAI 兼容的视觉接口（`/v1/chat/completions`）。支持本地 Ollama、LocalAI、DeepSeek、Qwen、Claude、GPT-4o 等多模态模型。
- 🧹 **纯净主时间线**：截图、表情包、广告杂图自动移至 Immich 归档，主时间线只留真实美好回忆。
- 📁 **自动相册整理**：发票扫描件进文档相册，证件凭证进证件相册，查阅调阅更便捷。
- ⚡ **实时触发 + 轮询双通道**：
  - **Webhook 实时触发**：配合 Immich Workflow，新图上传打标即刻触发分类；
  - **定时轮询保底**：后台定时扫描队列，杜绝漏图。
- 🔒 **并发安全与租约机制**：SQLite 原生原子租约锁，即使多 Worker 并发也绝不重复处理同一张图。
- ↩️ **一键撤销与审计回滚**：记录每一步 Immich 变更（`change_log`），支持对任意 Job 一键完全撤销回滚。
- ⭐ **评分与描述**：让视觉模型顺手补上 Immich 自带的星级评分与描述；逐字段判定，你自己写过的值绝不被覆盖。
- 🛡️ **安全隔离不误伤**：重置（Danger Zone）只处理你勾选的维度（标签 / 分类 / 评分 / 描述），且只对本系统处理过的图片生效；标签对象保留，绝不破坏用户自行管理的标签与相册。
- 🎨 **Linear 暗黑风控制面板**：基于 FastAPI + Tailwind CSS + Alpine.js + HTMX 构建，支持 SSE 实时进度刷新、卡片式相册浏览与全套可视化规则配置。

---

## 🚀 快速上手 (最小能跑 Demo)

只需 **3 分钟**，使用 Docker Compose 快速启动运行：

### 第一步：克隆代码仓库并配置 `.env`

镜像已在每次推送时自动构建并发布到 GHCR，因此你只需要 compose 文件和 `.env`，无需本地构建工具链（Python / Node / Tailwind）。

```bash
git clone git@github.com:PoweredByPassion/immich-llm-tagger.git
cd immich-llm-tagger
cp .env.example .env
```

编辑 `.env` 文件，填入你的 Immich 地址和视觉模型接口：

```dotenv
# Immich 服务端连接
IMMICH_URL=http://your-immich-ip:2283
IMMICH_API_KEY=your_immich_api_key

# OpenAI 兼容的视觉大模型接口
LLM_BASE_URL=http://host.docker.internal:7863  # 或你的大模型服务地址
LLM_API_KEY=your_llm_api_key
LLM_MODEL=deepseek-vl                         # 视觉模型名称
DATA_DIR=/data
AUTH_PASSWORD=                                # 设置后启用访问密码（公网部署建议开启）；留空则不启用认证
```

> **提示（宿主机模型服务）**：如果你的大模型部署在宿主机（例如 7863 端口或 11434 端口的 Ollama），保留 `host.docker.internal` 可以让容器内顺利访问宿主机网络。

### 第二步：一键启动服务

自动拉取预构建镜像 `ghcr.io/poweredbypassion/immich-llm-tagger:latest` 并启动容器：

```bash
docker compose up -d
```

启动完成后，打开浏览器访问：**`http://localhost:8080`**。

### 第三步：导入开箱即用规则

项目自带两套开箱即用的本地化规则模版：
- **`autotag-config.zh.yaml`**（推荐中文用户）：纯中文标签（`截图`、`文档`、`证件`、`表情包`、`生活照片`...），中文 Prompt，队列标签为 `未分类`；
- **`autotag-config.en.yaml`**（英文国际化）：英文标签（`screenshot`、`document`、`meme`...），英文 Prompt，队列标签为 `unclassified`。

**导入步骤**：
1. 进入 Web 控制台：**系统设置 -> 配置迁移**（`http://localhost:8080/config/transfer`）；
2. 点击**「导入配置」**，上传项目目录下的 [`autotag-config.zh.yaml`](autotag-config.zh.yaml)（或英文版）；
3. 瞬间载入全部标签定义、相册规则、归档阈值与 Prompt 模板！

---

## 🔌 Immich 端配置指南（两种触发模式）

Immich LLM Tagger 支持自动化和手动两种打标触发模式，可按你的使用习惯选择：

### 模式一：Immich Workflow 自动化工作流（新图免干预全自动）

在 Immich 中配置内置 Workflow，新照片上传时自动贴上 `未分类` 标签并通知 Tagger 处理：

1. 进入 Immich 管理员后台：**Administration -> Workflows**（Immich v1.118+ 支持）；
2. 点击 **Create Workflow**，命名为如 `Auto Classify`；
3. 设置 **Trigger（触发器）**：选择 `AssetCreate`（资产上传完成）或 `AssetMetadataExtraction`（元数据提取后）；
4. 添加 **Step 1 - Add Tag（加标签步骤）**：
   - Action：选择 `Add Tag`
   - Tag Name：填入 `未分类`（如果使用英文规则，填入 `unclassified`）；
5. 添加 **Step 2 - Trigger Webhook（触发 Webhook 步骤）**：
   - Action：选择 `Trigger Webhook`
   - Method：`POST`
   - URL：`http://<your-tagger-host>:8080/api/hooks/immich`
   - Custom Headers：
     ```http
     x-webhook-token: <在 Tagger Web 控制台「连接配置」中生成的 Webhook Token>
     ```
6. **最终效果**：手机拍摄新照片上传到 Immich 后，Immich 会自动贴上 `未分类` 标签并秒级推送 Webhook，Tagger 异步拉取缩略图、调用视觉大模型识别、执行归档或相册归集，并**自动摘除 `未分类` 标签**，全流程无需人工介入！

---

### 模式二：手动打标与存量历史照片处理

如果你不想开启自动工作流，或想分类整理以前备份的大量历史照片：

- **方法 A：在 Immich 网页端 / App 中手动批量打标**
  1. 打开 Immich 照片库；
  2. 鼠标框选需要处理的一批照片（或点击日期旁边的勾选按钮选择整天）；
  3. 点击右上角菜单的 **Tag (标签)** 按钮，添加 `未分类`（英文填 `unclassified`）；
  4. 如果配置了 `AssetTagged` Webhook，Tagger 会立即开跑；即使没有配置 Webhook，Tagger 的定时轮询器（默认每 5 分钟）也会自动扫描出来并处理，或者你可以在 Tagger 控制台直接点击「立即扫描」。

- **方法 B：在 Tagger 控制台一键全量回填 (Backfill)**
  1. 打开 Tagger 控制台页面（`http://localhost:8080`）；
  2. 进入 **处理范围 (Scope)** 或 **系统概览** 页面；
  3. 点击 **「全量回填 (Backfill)」** 按钮；
  4. 系统将根据你配置的库和路径范围，**批量自动为所有历史未处理的图片打上 `未分类` 标签**并排入处理队列。

---

## 🏷️ 默认中文规则明细 (`autotag-config.zh.yaml`)

| 标签名称 | 标签类型 | 处理动作 | 目标相册 | 最低置信度 | 分类判定标准 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`截图`** | 主标签 | **自动归档** | — | `0.80` | 手机/电脑屏幕截屏、社交聊天记录、地图导航、支付界面、系统设置、应用截屏。 |
| **`文档`** | 主标签 | 保留时间线 | **`文档资料`** | `0.75` | 纸质拍摄/扫描文档、发票收据、合同、表格、白板板书、手写便签、书本教材页面。 |
| **`证件`** | 主标签 | 保留时间线 | **`证件凭据`** | `0.85` | 身份证、护照、驾驶证、行驶证、银行卡、信用卡、社保卡、营业执照、资质凭证。 |
| **`表情包`** | 主标签 | **自动归档** | — | `0.80` | 网络表情包、搞笑梗图、社交斗图图片、带粗大字体的二创网图。 |
| **`杂图`** | 主标签 | **自动归档** | — | `0.85` | 营销广告海报、电商买家秀、应用图标Logo、误触全黑/全白模糊废片。 |
| **`生活照片`**| 主标签 | 保留时间线 | — | `0.70` | 相机/手机真实拍摄的人物、自拍、旅行风景、美食打卡、宠物、日常生活记录。 |
| **`其他`** | 主标签 | 保留时间线 | — | *(系统兜底)* | 画面主体不明确、多元素复合或模型未能准确归类的图片。 |
| **`敏感隐私`** | 标记标签 | 仅打标记 | — | *自动触发* | 识别出证件、银行卡、密码隐私，或模型安全层触发拒答时自动打上。 |
| **`低置信度`** | 标记标签 | 仅打标记 | — | *`< 0.80`* | 预测为归档类但置信度不足时打上，阻止自动归档并保留在时间线供人工抽检。 |
| **`待复核`** | 标记标签 | 仅打标记 | — | *`< 0.50`* | 置信度严重偏低，提示需人工在后台重新分类。 |

---

## ⭐ 评分与描述

除了标签，系统还能写入 Immich 自带的**星级评分**（1–5）与**描述**。视觉模型在返回标签的同时返回这两个字段，它们会与可见性变更合并为**同一次 `PUT /assets` 请求** —— 一次请求，每个字段各落一条 `change_log` 记录。

两个字段**各自独立判定**（同一张图的评分与描述可以走不同分支）：

| Immich 中的当前值 | 处理方式 |
| :--- | :--- |
| 空（从未评分 / 无描述） | 写入模型给出的值 |
| 等于本系统上次写入的值 | 普通重扫保持原样；仅当本次应用是显式发起（「重新分类」）时才刷新 |
| 你自己写的 | **永不覆盖** —— 跳过该字段，并给这张图打上*跳过标记*标签（默认 `保留人工信息`，英文默认 `manual-kept`），跳过原因记入 `change_log` |

已存在的 `rating = -1` 按「有值」处理（不等于「未评分」），这类图片会留给你自己处理。

### `enabled_effects`

每个写入目标都是一个独立的**效应器**：`tag`、`visibility`、`album`、`rating`、`description`。可在 **配置中心 → 定时轮询 → 效应器与重试** 中勾选（`enabled_effects`）；未勾选的效应器在规划阶段就被剔除 —— 既不写 Immich，也不落 `change_log`。默认五个全开。若想「静默升级」（继续打标签，但评分/描述留给你手填），把 `rating` / `description` 从列表中移除即可。

同一面板还有：

- **跳过标记标签名** —— 用户自己写过评分/描述而被跳过时打上的标签（留空则跟随界面语言）。
- **应用最大重试次数** —— 应用失败后重试到多少次就放弃（默认 `3`）。仍在等待重试的图片会在详情页显示*待重试*胶囊，始终未能应用的记录会出现在任务的失败列表中。

### 危险区：按维度重置

危险区现在可重置**四个互相独立的维度** —— 标签、分类（取消归档 + 移出相册）、评分、描述。每个勾选项旁显示**预估影响资产数**（基于本地数据库统计）。**只处理勾选的维度**，未勾选的维度原样不动（本地镜像列同样不动）。

评分/描述还带安全阀：只有当 Immich 的*当前值*仍等于本系统最后写入的值时，才还原为**本系统第一次写入之前的值**；若你此后自己改过，则跳过、不动你的编辑（原本为空的描述还原为 `""`）。重置仍需输入两次 `RESET` 确认，且有任何活动任务时会被拒绝。

---

## 🛠️ 高级功能

- **试分类沙盒 (Sandbox)**：输入任意图片 ID 或随机抽取，实时预览 LLM 分类结果与 Prompt 渲染，不向 Immich 写入任何数据。
- **重新分类 (Reclassify)**：调整了标签规则或换了新 Prompt？一键对指定图片摘除旧标签并重新分类重跑 —— 这也是唯一会刷新「本系统自己写过的评分/描述」的路径。
- **任务撤销 (Undo)**：在任务详情页面，支持对任何已完成任务一键撤销（摘掉新加标签、恢复队列标签、还原可见性）。撤销危险区重置时，仅恢复系统管理过图片上的标签，不还原可见性。
- **危险区重置 (Danger Zone)**：勾选一个或多个维度（标签 / 分类 / 评分 / 描述）后只重置这些维度，评分/描述的安全阀见上文**「评分与描述」**小节。标签对象不会被删除，其他图片上用户自行管理的标签也不受影响。

---

## 🧱 本地开发指南

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装开发依赖
pip install -e ".[dev]"

# 3. 生成 Immich OpenAPI 客户端代码
make generate

# 4. 构建 Tailwind CSS
make css

# 5. 运行全部单元测试 (980+ 自动化测试)
pytest
```

如需从源码本地构建镜像（请使用下面完全一致的 tag，`docker compose up -d` 才会优先使用本地构建的镜像）：

```bash
docker build -t ghcr.io/poweredbypassion/immich-llm-tagger:latest .
```

---

## ☕ 赞助支持

如果这个项目帮你省下了时间，欢迎请我喝一杯：

👉[Buy me a beer](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=M67JY6LTXNLB4&on0=donate&os0=Buy+Me+A+Beer)

👉[Buy me a vps](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=M67JY6LTXNLB4&on0=donate&os0=Buy+Me+A+VPS)

👉[Buy me a keyboard](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=M67JY6LTXNLB4&on0=donate&os0=Buy+Me+A+Keyboard)

---

## 📄 开源许可

本项目基于 [MIT License](LICENSE) 开源。
