"""Configuração da aplicação — lida de variáveis de ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Carrega o .env do diretório atual (raiz do repo), se existir.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    model: str
    max_steps: int
    max_duration_seconds: int
    prompt_path: Path
    data_dir: Path


def load_settings() -> Settings:
    return Settings(
        model=os.getenv("AGENTE_QA_MODEL", "sonnet"),
        max_steps=int(os.getenv("AGENTE_QA_MAX_STEPS", "40")),
        max_duration_seconds=int(os.getenv("AGENTE_QA_MAX_DURATION_SECONDS", "600")),
        prompt_path=Path(os.getenv("AGENTE_QA_PROMPT_PATH", "prompts/system_prompt.md")),
        data_dir=Path(os.getenv("AGENTE_QA_DATA_DIR", "data")),
    )


settings = load_settings()
