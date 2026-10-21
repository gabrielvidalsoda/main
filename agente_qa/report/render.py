"""Gera o relatório final em Markdown, no mesmo esqueleto dos relatórios manuais
do Gabriel (resumo numérico, bugs no modelo fixo de defeito, seção separada de
dúvidas/sugestões) — ver §7 do design técnico."""

from __future__ import annotations

from ..agent.tools import Finding
from ..storage.files import RunPaths
from ..storage.test_spec import TestSpec


def _severity_counts(findings: list[Finding]) -> dict[int, int]:
    counts = {1: 0, 2: 0, 3: 0}
    for f in findings:
        if f.kind == "bug" and f.severity in counts:
            counts[f.severity] += 1
    return counts


def render_report(
    *,
    paths: RunPaths,
    test_spec: TestSpec,
    run_id: str,
    mode: str,
    prompt_hash: str,
    started_at: str,
    outcome: str,
    summary: str,
    steps_executed: int,
    findings: list[Finding],
    questions: list[str],
) -> str:
    bugs = [f for f in findings if f.kind == "bug"]
    observations = [f for f in findings if f.kind == "observacao"]
    suggestions = [f for f in findings if f.kind == "sugestao"]
    sev_counts = _severity_counts(bugs)

    lines: list[str] = []
    lines.append(f"# Relatório de Execução — {test_spec.label}")
    lines.append("")
    lines.append("| Item | Valor |")
    lines.append("|---|---|")
    lines.append(f"| Teste | `{test_spec.path}` |")
    lines.append(f"| Execução | `{run_id}` — {started_at} |")
    lines.append(f"| Modo | {mode} |")
    lines.append(f"| URL alvo | {test_spec.target_url} |")
    lines.append(f"| Resultado | **{outcome}** |")
    lines.append(f"| Versão do prompt | `{prompt_hash}` |")
    lines.append("")

    lines.append("## Resumo")
    lines.append("")
    lines.append(f"- Passos executados: {steps_executed}")
    lines.append(
        f"- Bugs encontrados: {len(bugs)} "
        f"(severidade 1: {sev_counts[1]}, 2: {sev_counts[2]}, 3: {sev_counts[3]})"
    )
    lines.append(f"- Observações: {len(observations)}")
    lines.append(f"- Sugestões: {len(suggestions)}")
    lines.append("")
    lines.append(f"**Resumo do agente:** {summary}")
    lines.append("")

    lines.append("## Bugs")
    lines.append("")
    if not bugs:
        lines.append("Nenhum bug encontrado.")
    for f in bugs:
        lines.append(f"### BUG-{run_id}-{f.index:02d} — {f.title}")
        lines.append("")
        lines.append(f"- **Severidade:** {f.severity}")
        lines.append(f"- **Passos para reproduzir:**")
        for i, step in enumerate(f.steps_to_reproduce, start=1):
            lines.append(f"  {i}. {step}")
        lines.append(f"- **Esperado:** {f.expected}")
        lines.append(f"- **Observado:** {f.observed}")
        if f.screenshot_path:
            lines.append(f"- **Evidência:** ![]({f.screenshot_path})")
        lines.append("")

    lines.append("## Observações (sem evidência suficiente para confirmar como bug)")
    lines.append("")
    if not observations:
        lines.append("Nenhuma observação registada.")
    for f in observations:
        lines.append(f"- **{f.title}** — esperado: {f.expected}; observado: {f.observed}."
                      + (f" Evidência: ![]({f.screenshot_path})" if f.screenshot_path else ""))
    lines.append("")

    lines.append("## Sugestões")
    lines.append("")
    if not suggestions:
        lines.append("Nenhuma sugestão registada.")
    for f in suggestions:
        lines.append(f"- **{f.title}** — {f.observed}")
    lines.append("")

    lines.append("## Dúvidas")
    lines.append("")
    if not questions:
        lines.append("Nenhuma dúvida registada.")
    for q in questions:
        lines.append(f"- {q}")
    lines.append("")

    content = "\n".join(lines)
    paths.report_md.write_text(content, encoding="utf-8")
    return str(paths.report_md)
