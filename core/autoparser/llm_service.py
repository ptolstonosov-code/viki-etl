"""
LLM service — manages the lifecycle of llama-server on the ARM device.

Three states:
  - STOPPED  — process not running, 0 MB RAM used
  - STARTING — load weights from disk (~10s on ARM)
  - READY    — accepting requests on http://127.0.0.1:8080

The service auto-stops itself after N seconds of inactivity (idle timeout).
This is critical on ARM 4 GB where we can't afford a 1.5 GB resident process
when no work is needed.

Two backends supported:
  - systemd  → use `systemctl start/stop llm-etl-llm` (production)
  - subprocess → spawn llama-server directly (dev / non-systemd hosts)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


class LLMService:
    """Lifecycle wrapper around llama-server. Lazy start, idle stop."""

    def __init__(
        self,
        *,
        binary: Path | str = "/opt/llm-etl/llama-server",
        model: Path | str = "/opt/llm-etl/models/active.gguf",
        port: int = 8080,
        threads: int = 4,
        ctx_size: int = 16384,
        idle_timeout: int = 60,
        use_systemd: bool = True,
        systemd_unit: str = "llm-etl-llm.service",
    ):
        self.binary = Path(binary)
        self.model = Path(model)
        self.port = port
        self.threads = threads
        self.ctx_size = ctx_size
        self.idle_timeout = idle_timeout
        self.use_systemd = use_systemd and shutil.which("systemctl") is not None
        self.systemd_unit = systemd_unit

        self._proc: subprocess.Popen | None = None
        self._last_used: float = 0.0
        self._lock = threading.Lock()
        self._idle_thread: threading.Thread | None = None

    # ── State queries ────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def is_running(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=2) as r:
                return r.status == 200
        except urllib.error.URLError:
            pass
        try:
            with urllib.request.urlopen(f"{self.url}/v1/models", timeout=2) as r:
                return r.status == 200
        except urllib.error.URLError:
            return False
        except Exception:
            return False

    # ── Start / stop ─────────────────────────────────────────────────────────

    def start(self, wait_timeout: int = 60) -> None:
        with self._lock:
            if self.is_running():
                self._last_used = time.time()
                return
            if self.use_systemd:
                logger.info("starting %s via systemctl", self.systemd_unit)
                subprocess.run(["systemctl", "start", self.systemd_unit], check=True)
            else:
                logger.info("starting llama-server as subprocess")
                cmd = [
                    str(self.binary),
                    "--model", str(self.model),
                    "--port", str(self.port),
                    "--host", "127.0.0.1",
                    "--ctx-size", str(self.ctx_size),
                    "--threads", str(self.threads),
                    "--batch-size", "256",
                    "--no-mmap",
                    "--n-gpu-layers", "0",
                ]
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Wait for HTTP up
            t0 = time.time()
            while time.time() - t0 < wait_timeout:
                if self.is_running():
                    logger.info("llama-server ready after %.1fs", time.time() - t0)
                    self._last_used = time.time()
                    self._ensure_idle_watcher()
                    return
                time.sleep(1)
            raise RuntimeError(f"llama-server didn't become ready in {wait_timeout}s")

    def stop(self) -> None:
        with self._lock:
            if self.use_systemd:
                logger.info("stopping %s via systemctl", self.systemd_unit)
                subprocess.run(["systemctl", "stop", self.systemd_unit],
                               check=False)
            elif self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None

    # ── Idle watcher ─────────────────────────────────────────────────────────

    def _ensure_idle_watcher(self):
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return

        def _watch():
            while True:
                time.sleep(15)
                if not self.is_running():
                    return
                idle_for = time.time() - self._last_used
                if idle_for > self.idle_timeout:
                    logger.info("LLM idle for %.0fs — stopping", idle_for)
                    try:
                        self.stop()
                    except Exception as e:
                        logger.warning("idle stop failed: %s", e)
                    return

        self._idle_thread = threading.Thread(target=_watch, daemon=True)
        self._idle_thread.start()

    # ── Inference call ───────────────────────────────────────────────────────

    def chat(self, system: str, user: str,
             max_tokens: int = 2048, temperature: float = 0.0,
             cache_prompt: bool = True) -> str:
        """Send one chat request. Auto-starts the service if needed."""
        if not self.is_running():
            self.start()
        self._last_used = time.time()

        body = json.dumps({
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "cache_prompt": cache_prompt,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode("utf-8"))
        self._last_used = time.time()
        return resp["choices"][0]["message"]["content"]
