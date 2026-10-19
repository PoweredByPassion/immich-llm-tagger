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
- ⭐ **Ratings & Descriptions (switched per tag)**: Let the vision model also fill in Immich's own star rating and description. **Each tag decides whether they are written** (the same model as archive / album / skip), the rating means "how good the photo looks" rather than importance or keepsake value, and a per-field guard never overwrites something you wrote yourself.
- 🛡️ **Non-destructive & Scoped Safety**: The Danger Zone resets only the dimensions you check (tags / classification / rating / description) and only touches assets this system has handled—tag objects and your manually curated tags on other photos remain completely untouched.
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
3. All tags (including the **Write rating** / **Write description** switches and the archive/album rules), thresholds, and prompt templates are instantly configured!

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
     x-webhook-token: <Your Webhook Token from Tagger UI -> Connections -> Webhook Endpoint>
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
| **`refused`** | Marker | Marker Only | — | *Automatic* | Added when the model refuses to answer (or returns unparseable output) so no classification was produced — filter on it to review the failures manually. |

> Whether rating/description are written is a per-tag switch too: in the table above, **archived tags default to `write_rating: false`** while still writing the description; every other tag writes both. See "Rating / description: controlled per tag" below.

---

## ⭐ Rating & Description

Besides tags, the tagger can write Immich's built-in **star rating** (1–5) and **description** onto each asset. The vision model returns both together with the tag, and they are folded together with any visibility change into a **single `PUT /assets` request** — one request, one committed `change_log` entry per field. **Whether they are written at all is decided per tag** (next section); the table below describes what happens *after* a tag opts in.

**The rating means "how good the photo looks"** — judged only on composition, exposure, focus sharpness, colour, lighting, subject prominence and tidiness. It is *not* importance, keepsake or emotional value: important people, events or dates never raise the score (the filename and capture date are context only). `confidence` is the opposite — it expresses certainty about the *classification*, not how good the photo is. These rules ship inside the default prompts (`autotag-config.en.yaml` / `.zh.yaml`) and can be edited under **Config → Prompt Versions**.

> **Upgrading**: the shipped prompt now defines `rating` as image quality (it used to mean importance / keepsake value), and the rating/description switches moved from a global setting to the per-tag columns (both default to on). Existing installs get the new prompt by re-importing the YAML or pasting the new body into **Prompt Versions** and activating it; historical ratings keep their old meaning and are never recomputed — to refresh them, run **Reclassify** on the affected assets (the only path that rewrites values this system wrote).

Each field is decided independently (a rating and a description on the same asset can take different branches):

| Current value in Immich | What happens |
| :--- | :--- |
| empty (never rated / no description) | the model's value is written |
| equals the value this system wrote last time | kept as-is on a normal re-scan; refreshed only when the apply was explicitly initiated (Reclassify) |
| written by you | **never overwritten** — the field is skipped and the asset gets the *skip-marker* tag (default `manual-kept`; Chinese default `保留人工信息`), with the skipped field recorded in `change_log` |

An existing `rating = -1` counts as a value (it is not treated as "unrated") — such an asset is left to you.

### Rating / description: controlled per tag

**Whether rating and description are written is decided by the tag system** — the same model as *archive* (`archive`), *album* (`album`) and *skip Immich* (`skip_immich`). In **Config → Tags**, every tag has two independent checkboxes, **Write rating** and **Write description** (both on by default). Only assets matching that tag get those fields written; a tag with the box unchecked writes nothing, logs nothing to `change_log`, and does *not* get the skip-marker tag (that marker means "we wanted to write, but your value was in the way" — not "we never intended to write").

**What "Skip" covers**: `skip_immich` blocks **tags / album / archive** only — it does *not* affect rating and description, which have their own switches. So the typical setup is `normal_photo` with *Skip* + *Write rating* + *Write description* checked: your real photos get no tag and no archive, but do get rated and described (which is then the only write that tag performs). To leave a class of images completely untouched in Immich, turn all three off. The rating/description skip rules still respect a manual override.

**What the shipped config does**: archived tags (`screenshot`, `meme`, `junk`) keep the description but skip the rating — archiving means "hide from the main timeline", where a star rating is meaningless, while the description still makes the asset searchable. Every other tag writes both. These are just starting values; change them any time under Tags (e.g. turn the description off for `id_document` to keep sensitive text out of that field).

### `enabled_effects` (global effectors)

Every write target is an independent effector, but `tag`, `visibility` and `album` are **global** switches, editable in **Config → Recognition Settings → Effectors & Retries** (`enabled_effects`); anything not listed is planned away entirely — no Immich write and no `change_log` entry. All three default to on.

`rating` / `description` are **not** in that list — they moved to the per-tag switches above. Upgrade note: if a previous version had `rating` / `description` removed from `enabled_effects`, those fields will start being written again after upgrading; to stop them, uncheck the per-tag switches.

The same panel contains:

- **Skip-marker tag name** — the tag applied when a user-authored rating/description was skipped (empty = follow the UI language).
- **Max apply attempts** — how often a failed apply is retried before the asset is given up on (default `3`). An asset still awaiting a retry shows a *retry* pill in its detail view, and records that never got applied are listed in the job's failure list.

### Danger Zone: per-dimension reset

The Danger Zone resets **four independent dimensions** — tags, classification (unarchive + remove from albums), rating, description. Each checkbox shows the *estimated* number of affected assets, computed from the local database. **Only the checked dimensions are touched**; an unchecked dimension is left exactly as it is, local mirror columns included.

For rating/description the reset has a safety valve: a field is restored to the value that existed before this system first wrote it — and only if Immich's *current* value still equals what the system last wrote. If you edited it yourself since, the reset leaves it alone (an originally empty description is restored as `""`). Reset still requires typing `RESET` twice and is refused while any job is active.

---

## 🛠️ Advanced Operations

- **Single Asset Processing & Sandbox**: Test LLM prompt results on any single `assetId` in real-time without writing changes to Immich.
- **Reclassify (重新分类)**: If you update your prompt or category rules, easily trigger a reclassify on specific assets. The system strips previous managed tags and re-evaluates the asset — this is also the one path that refreshes a rating/description it wrote itself.
- **Task Rollback (Undo)**: In the **Jobs** tab, click **Undo** on any finished job to reverse the tag additions, album attachments, and visibility changes made during that run. (Undoing a Danger Zone reset restores tags on system-managed assets; visibility stays unarchived.)
- **Danger Zone Reset (安全重置)**: Pick one or more dimensions (tags / classification / rating / description) and reset only those — see **Rating & Description** above for the rating/description safety valve. Tag objects are never deleted and user-curated tags on other photos are never touched.

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

## ☕ Donate

If this project saved you some time, consider buying me a drink:

👉[Buy me a beer](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=M67JY6LTXNLB4&on0=donate&os0=Buy+Me+A+Beer)

👉[Buy me a vps](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=M67JY6LTXNLB4&on0=donate&os0=Buy+Me+A+VPS)

👉[Buy me a keyboard](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=M67JY6LTXNLB4&on0=donate&os0=Buy+Me+A+Keyboard)

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
