"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # ---- LLM (OpenAI-compatible, e.g. vLLM serving Qwen) ----
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://172.28.26.96:9192/v1").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "EMPTY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))  # empty = auto-detect from /models
    vision_model: str = field(default_factory=lambda: _env("VISION_MODEL", ""))  # empty = same as llm_model
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 300))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 4096))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.2))
    # Qwen3 hybrid thinking: off by default (faster, cleaner JSON). Set 1 to allow thinking.
    llm_enable_thinking: bool = field(default_factory=lambda: _env_bool("LLM_ENABLE_THINKING", False))
    use_vision: bool = field(default_factory=lambda: _env_bool("USE_VISION", True))

    # ---- Storage ----
    output_dir: Path = field(default_factory=lambda: Path(_env("OUTPUT_DIR", "/data/outputs")))
    projects_dir: Path = field(default_factory=lambda: Path(_env("PROJECTS_DIR", "/workspace")))

    # ---- Browser ----
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))
    chromium_executable: str = field(default_factory=lambda: _env("CHROMIUM_EXECUTABLE", ""))
    viewport_width: int = field(default_factory=lambda: _env_int("VIEWPORT_WIDTH", 1280))
    viewport_height: int = field(default_factory=lambda: _env_int("VIEWPORT_HEIGHT", 720))
    locale: str = field(default_factory=lambda: _env("BROWSER_LOCALE", "zh-TW"))
    ignore_https_errors: bool = field(default_factory=lambda: _env_bool("IGNORE_HTTPS_ERRORS", True))
    action_timeout_ms: int = field(default_factory=lambda: _env_int("ACTION_TIMEOUT_MS", 10000))
    nav_timeout_ms: int = field(default_factory=lambda: _env_int("NAV_TIMEOUT_MS", 30000))

    # ---- Tutorial / video ----
    tts_engine: str = field(default_factory=lambda: _env("TTS_ENGINE", "none").lower())  # none | espeak | openai
    tts_base_url: str = field(default_factory=lambda: _env("TTS_BASE_URL", "").rstrip("/"))
    tts_api_key: str = field(default_factory=lambda: _env("TTS_API_KEY", "EMPTY"))
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", "tts-1"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", ""))
    espeak_voice: str = field(default_factory=lambda: _env("ESPEAK_VOICE", "cmn"))
    caption_chars_per_sec: float = field(default_factory=lambda: _env_float("CAPTION_CHARS_PER_SEC", 5.0))
    min_step_seconds: float = field(default_factory=lambda: _env_float("MIN_STEP_SECONDS", 2.5))
    typing_delay_ms: int = field(default_factory=lambda: _env_int("TYPING_DELAY_MS", 60))

    # ---- Server ----
    host: str = field(default_factory=lambda: _env("MCP_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("MCP_PORT", 8000))
    # Base URL that clients use to open generated files, e.g. http://10.0.0.5:8000
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "").rstrip("/"))
    max_concurrent_browsers: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_BROWSERS", 2))

    def artifact_url(self, path: Path) -> str | None:
        """Map a file under output_dir to an HTTP URL served by /files."""
        if not self.public_base_url:
            return None
        try:
            rel = Path(path).resolve().relative_to(self.output_dir.resolve())
        except ValueError:
            return None
        return f"{self.public_base_url}/files/{rel.as_posix()}"


settings = Settings()
