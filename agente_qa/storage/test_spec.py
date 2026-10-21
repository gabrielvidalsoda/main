"""Leitura e validação do arquivo YAML de definição de um teste."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class TestSpecError(ValueError):
    """Erro de sintaxe/validação num arquivo de teste."""


@dataclass(frozen=True)
class TestSpec:
    path: Path
    label: str
    target_url: str
    steps: list[str]
    evaluation: str
    tags: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.path.stem


REQUIRED_FIELDS = ("label", "target_url", "steps", "evaluation")


def load_test_spec(path: Path) -> TestSpec:
    """Lê e valida um arquivo YAML de teste. Levanta TestSpecError com uma mensagem
    clara se algo estiver faltando ou no formato errado — pensado para ser chamado
    tanto por `agente-qa run` quanto por `agente-qa validate`.
    """
    if not path.exists():
        raise TestSpecError(f"Arquivo de teste não encontrado: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TestSpecError(f"YAML inválido em {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise TestSpecError(f"{path}: o arquivo precisa ser um mapeamento YAML (chave: valor).")

    missing = [f for f in REQUIRED_FIELDS if f not in raw or raw[f] in (None, "")]
    if missing:
        raise TestSpecError(
            f"{path}: campos obrigatórios ausentes ou vazios: {', '.join(missing)}."
        )

    steps = raw["steps"]
    if not isinstance(steps, list) or not steps:
        raise TestSpecError(f"{path}: 'steps' precisa ser uma lista não-vazia de strings.")
    steps = [str(s).strip() for s in steps]

    target_url = str(raw["target_url"]).strip()
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        raise TestSpecError(
            f"{path}: 'target_url' precisa ser uma URL absoluta (http:// ou https://)."
        )

    tags_raw = raw.get("tags") or []
    if not isinstance(tags_raw, list):
        raise TestSpecError(f"{path}: 'tags' precisa ser uma lista de strings.")
    tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())

    return TestSpec(
        path=path,
        label=str(raw["label"]).strip(),
        target_url=target_url,
        steps=steps,
        evaluation=str(raw["evaluation"]).strip(),
        tags=tags,
    )


def discover_test_specs(
    path: Path, tags: list[str] | None = None
) -> tuple[list[TestSpec], list[tuple[Path, str]]]:
    """Resolve um caminho num ou mais `TestSpec`, para rodar/validar em lote.

    `path` pode ser um arquivo `.yaml` único (comportamento igual a
    `load_test_spec`, erro aborta na hora — preserva o comportamento anterior a
    esta função) ou uma pasta, caso em que todos os `*.yaml`/`*.yml` dela são
    lidos recursivamente em ordem alfabética; um arquivo malformado dentro da
    pasta não derruba os demais — vem de volta na lista `errors`.

    `tags`, se informado, filtra o resultado para specs que tenham QUALQUER uma
    das tags dadas (campo opcional `tags:` no YAML).
    """
    if not path.exists():
        raise TestSpecError(f"Caminho de teste não encontrado: {path}")

    wanted = {t.strip() for t in tags if t and t.strip()} if tags else None

    if not path.is_dir():
        spec = load_test_spec(path)
        if wanted and not (wanted & set(spec.tags)):
            return [], []
        return [spec], []

    files = sorted(set(path.rglob("*.yaml")) | set(path.rglob("*.yml")))
    if not files:
        raise TestSpecError(f"Nenhum arquivo .yaml encontrado em {path}.")

    specs: list[TestSpec] = []
    errors: list[tuple[Path, str]] = []
    for f in files:
        try:
            specs.append(load_test_spec(f))
        except TestSpecError as exc:
            errors.append((f, str(exc)))

    if wanted:
        specs = [s for s in specs if wanted & set(s.tags)]

    return specs, errors
