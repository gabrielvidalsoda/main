"""Carrega o prompt de sistema editável (o "manual do QA")."""

from __future__ import annotations

import hashlib
from pathlib import Path


class PromptNotFoundError(FileNotFoundError):
    pass


def load_system_prompt(path: Path) -> tuple[str, str]:
    """Retorna (conteúdo, hash_curto). O hash entra no run.json de cada execução
    (RNF-08 do documento de requisitos) para saber com qual versão do prompt aquele
    relatório foi gerado — sem precisar de um sistema de versionamento próprio,
    já que o arquivo é versionado pelo git."""
    if not path.exists():
        raise PromptNotFoundError(
            f"Prompt de sistema não encontrado em {path}. "
            "Configure AGENTE_QA_PROMPT_PATH ou restaure prompts/system_prompt.md."
        )
    content = path.read_text(encoding="utf-8")
    short_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return content, short_hash
