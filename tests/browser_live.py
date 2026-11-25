"""Run with python -m tests.browser_live after installing Playwright's Chromium."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

from app.db.connection import Database
from app.db.migrate import migrate
from app.store.settings import SettingsStore


def main() -> None:
    with TemporaryDirectory() as data_dir:
        db = Database(Path(data_dir) / "autotag.db")
        migrate(db)
        SettingsStore(db).set("poll_enabled", False)
        env = dict(os.environ, DATA_DIR=data_dir, AUTH_PASSWORD="")
        for name in ("IMMICH_URL", "IMMICH_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
            env[name] = ""
        with subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.web.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", "0"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        ) as server:
            try:
                assert server.stderr is not None
                for line in server.stderr:
                    match = re.search(r"Uvicorn running on (http://127\.0\.0\.1:\d+)", line)
                    if match:
                        check_live_pages(match[1], db)
                        break
                else:
                    raise RuntimeError("Could not start Tagger for the browser test")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()


def check_live_pages(url: str, db: Database) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
        )
        try:
            page = browser.new_page(base_url=url)
            page.add_init_script("""
                window.streams = [];
                window.EventSource = class extends EventSource {
                    constructor(...args) {
                        super(...args);
                        window.streams.push(this);
                    }
                };
            """)
            page.goto("/")
            metric = page.locator("[data-overview-processed]")
            expect(metric).to_have_text("0")
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO asset_records (asset_id, source, status, applied_at) "
                    "VALUES ('browser-test', 'manual', 'ok', datetime('now'))",
                )
                job_id = conn.execute(
                    "INSERT INTO jobs (type, status, total, started_at) "
                    "VALUES ('scan', 'running', 3, datetime('now'))",
                ).lastrowid
            expect(metric).to_have_text("1")
            page.goto(f"/jobs/{job_id}")
            progress = page.locator("[data-job-progress]")
            expect(progress).to_have_text("0/3")
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET done = 1 WHERE id = ?", (job_id,))
            expect(progress).to_have_text("1/3")
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'done', done = 3, finished_at = datetime('now') "
                    "WHERE id = ?", (job_id,),
                )
            expect(progress).to_have_text("3/3")
            page.wait_for_function("window.streams[0]?.readyState === EventSource.CLOSED")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
    print("The overview and job pages refreshed in Chromium, and the completed job closed its stream.")
