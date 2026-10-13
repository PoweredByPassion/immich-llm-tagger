<div align="center">

<img src="logo.svg" alt="Immich LLM Tagger Logo" width="128" height="128" style="aspect-ratio: 1/1; object-fit: contain; border-radius: 20px;" />

# Immich LLM Tagger

**Automated Photo Classification, Smart Tagging, Album Filing & Timeline Cleaner for Immich powered by Vision LLMs.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](pyproject.toml)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](docker-compose.yml)
[![FastAPI](https://img.shields.io/badge/FastAPI-Framework-green.svg)](https://fastapi.tiangolo.com)

**[English](README.md)** | **[简体中文](README.zh-CN.md)**

</div>

---

## 📖 Overview

When backing up photos from smartphones (e.g. via iCloud sync, Google Photos, or Immich mobile app), personal timelines are frequently flooded with temporary clutter: **screen captures, receipts, invoices, IDs, reaction memes, and marketing junk**.

**Immich LLM Tagger** is a self-hosted, lightweight automation service designed specifically for Immich. It uses multimodal vision models (such as DeepSeek-VL, Qwen-VL, GPT-4o, Ollama, or any OpenAI-compatible Vision API) to:

1. **Auto-archive junk**: Move screenshots, memes, and throwaway graphics out of your main timeline into Immich Archive.
2. **Organize into Albums**: File invoices and paper scans into `Documents`, and identity certificates into `ID Documents`.
3. **Protect privacy**: Mark identity documents, passwords, and sensitive cards with `sensitive` tags.
4. **Preserve real photos**: Keep genuine life photography untouched on the main timeline.
5. **Full Reversibility**: Every single action is tracked in audit change logs and can be rolled back in one click.

---

## ✨ Features

- 🧠 **Multimodal Vision Intelligence**: Compatible with any OpenAI-compatible Vision API endpoint (`/v1/chat/completions`). Supports models like DeepSeek-VL, Qwen2.5-VL, MiniCPM, GPT-4o, Claude, or local Ollama instances.
- 🧹 **Pure Main Timeline**: Only keep real photography on your timeline. Clutter is safely moved to the Immich Archive.
- 📁 **Automated Album Filing**: Categorize documents and identity cards into dedicated Immich albums automatically.
- ⚡ **Dual Trigger Architecture**:
  - **Real-time Webhook**: Triggered immediately when photos are uploaded or tagged in Immich.
  - **Polling Fallback**: Configurable periodic scanning to ensure zero missed assets.
- 🔒 **Concurrency & Lease Lock**: Atomic lease claims in SQLite ensure no duplicate processing even with concurrent workers.
- ↩️ **One-Click Undo & Reversible Operations**: Full audit logging (`change_log`) allows rolling back any job at any time.
- 🛡️ **Non-destructive & Scoped Safety**: The Danger Zone reset only untags assets this system has tagged and unarchives photos archived by the system—tag objects and your manually curated tags on other photos remain completely untouched.
- 🎨 **Linear Dark Management UI**: Built with FastAPI, Tailwind CSS, Alpine.js, and HTMX. Features real-time SSE progress updates, asset cards, and visual rule editors.

---

## 🚀 Quick Start (Minimal Runnable Demo)

Run a complete instance in **3 minutes** using Docker Compose.

### Step 1: Clone Repository & Create `.env`

The image is prebuilt and published to GHCR on every push, so you only need the compose file and `.env` — no local build toolchain (Python / Node / Tailwind) required.

```bash
git clone git@github.com:PoweredByPassion/immich-llm-tagger.git
cd immich-llm-tagger
cp .env.example .env
```

Edit `.env` with your Immich instance and Vision LLM credentials:

```dotenv
# Immich Server Connection
IMMICH_URL=http://your-immich-ip:2283
IMMICH_API_KEY=your_immich_api_key

# OpenAI-Compatible Vision LLM Endpoint
LLM_BASE_URL=http://host.docker.internal:7863  # Or your LLM provider URL
LLM_API_KEY=your_llm_api_key
LLM_MODEL=deepseek-vl                         # Your vision model name
DATA_DIR=/data
AUTH_PASSWORD=                                # Set to enable password auth (public deployments); empty = disabled
```

> **Note for Local LLM Services**: If your LLM service runs directly on your Docker host machine (e.g. port 7863 or 11434 for Ollama), keep `host.docker.internal` so the container can reach the host network.

### Step 2: Start Service

Pulls the prebuilt image `ghcr.io/poweredbypassion/immich-llm-tagger:latest` and starts the container:

```bash
docker compose up -d
```

Open your browser and navigate to: **`http://localhost:8080`**.

### Step 3: Import Ready-to-Use Rules

The repository provides two localized rule templates:
- **`autotag-config.en.yaml`**: Standard English tags (`screenshot`, `document`, `meme`...) & English prompt (queue tag: `unclassified`).
- **`autotag-config.zh.yaml`**: Standard Chinese tags (`截图`, `文档`, `表情包`...) & Chinese prompt (queue tag: `未分类`).

**How to Import**:
1. In the Web UI, go to **System -> Config Transfer** (`http://localhost:8080/config/transfer`).
2. Click **Import Config**, select [`autotag-config.en.yaml`](autotag-config.en.yaml) (or [`autotag-config.zh.yaml`](autotag-config.zh.yaml)), and submit.
3. All tags, album filing rules, archive thresholds, and prompt templates are instantly configured!

---

## 🔌 Immich-Side Configuration (2 Trigger Modes)

Immich LLM Tagger supports two triggering workflows. Choose the one that best suits your setup:

### Mode 1: Automated Workflow (Zero-Touch for New Uploads)

Configure Immich's built-in Workflow to automatically tag new uploads as `unclassified` and notify Tagger:

1. In Immich Web UI, go to **Administration -> Workflows** (Immich v1.118+).
2. Click **Create Workflow**, name it `Auto Classify`.
3. Set **Trigger**: Select `AssetCreate` (when upload finishes) or `AssetMetadataExtraction`.
4. Add **Step 1 - Add Tag**:
   - Action: `Add Tag`
   - Tag Name: Enter `unclassified` (or `未分类` if using Chinese config).
5. Add **Step 2 - Trigger Webhook**:
   - Action: `Trigger Webhook`
   - Method: `POST`
   - URL: `http://<your-tagger-host>:8080/api/hooks/immich`
   - Custom Headers:
     ```http
     x-webhook-token: <Your Webhook Token from Tagger UI -> Settings>
     ```
6. **Result**: Any new photo uploaded from phones will be tagged as `unclassified`, trigger the Webhook immediately, get analyzed by the Vision LLM, filed/archived accordingly, and finally have the `unclassified` queue tag stripped away cleanly!

---

### Mode 2: Manual Tagging & Existing Photo Library

If you prefer manual control or want to organize your existing historical photos:

- **Method A: Manual Tagging in Immich UI**
  1. Open your Immich library in browser or mobile app.
  2. Select any photos you want to classify (or multi-select an entire day/album).
  3. Click the **Tag** icon and add the `unclassified` (or `未分类`) tag.
  4. If you have an `AssetTagged` Webhook configured, classification begins instantly; otherwise, the background polling scheduler (default every 5 minutes) will pick them up automatically. You can also click **Scan Now** in the Tagger UI to process immediately.

- **Method B: One-Click Library Backfill from Tagger UI**
  1. Open the Tagger Web UI (`http://localhost:8080`).
  2. Navigate to **Scope** or **Overview** page.
  3. Click **Backfill** (全量回填).
  4. The system will scan all assets matching your library/path scope rules and automatically tag all unhandled images with `unclassified`, queuing them for classification.

---

## 🏷️ Standard Tag Rules Reference

| Tag | Type | Action | Target Album | Min Confidence | Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`screenshot`** | Primary | **Archive** | — | `0.80` | Phone/PC screen captures, chat logs, maps, app UI, game HUDs. |
| **`document`** | Primary | Keep Timeline | **`Documents`** | `0.75` | Invoices, receipts, contracts, whiteboard notes, printed forms, book pages. |
| **`id_document`** | Primary | Keep Timeline | **`ID Documents`** | `0.85` | Passports, ID cards, driver licenses, bank cards, official credentials. |
| **`meme`** | Primary | **Archive** | — | `0.80` | Internet reaction images, stickers, memes with bold text. |
| **`junk`** | Primary | **Archive** | — | `0.85` | Throwaway ads, app icons, logos, accidental blurry shots, black/white frames. |
| **`normal_photo`**| Primary | Keep Timeline | — | `0.70` | Real-world photography (family, portraits, pets, scenery, food, travel). |
| **`other`** | Primary | Keep Timeline | — | *(Fallback)* | Ambiguous or multi-subject pictures requiring manual inspection. |
| **`sensitive`** | Marker | Marker Only | — | *Automatic* | Triggered automatically on IDs, bank details, passwords, or model safety refusals. |
| **`low_confidence`**| Marker| Marker Only | — | *`< 0.80`* | Added when predicted archive confidence is below threshold; prevents auto-archive. |
| **`needs_review`** | Marker | Marker Only | — | *`< 0.50`* | Added when confidence is very low, recommending manual review. |

---

## 🛠️ Advanced Operations

- **Single Asset Processing & Sandbox**: Test LLM prompt results on any single `assetId` in real-time without writing changes to Immich.
- **Reclassify (重新分类)**: If you update your prompt or category rules, easily trigger a reclassify on specific assets. The system strips previous managed tags and re-evaluates the asset.
- **Task Rollback (Undo)**: In the **Jobs** tab, click **Undo** on any finished job to reverse the tag additions, album attachments, and visibility changes made during that run. (Undoing a Danger Zone reset restores tags on system-managed assets; visibility stays unarchived.)
- **Danger Zone Reset (安全重置)**: Untag all assets previously tagged by the system and reset photos archived by it back to the timeline, without deleting tag objects or touching user-curated tags.

---

## 🧱 Local Development

```bash
# 1. Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Generate Immich OpenAPI client
make generate

# 4. Build Tailwind CSS
make css

# 5. Run test suite
pytest
```

To build the Docker image from source instead of pulling the prebuilt one (tag it exactly as below so `docker compose up -d` picks up the local build):

```bash
docker build -t ghcr.io/poweredbypassion/immich-llm-tagger:latest .
```

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
