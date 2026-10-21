"""CLI do Agente QA — ponto de entrada único, sem servidor, sem UI web (§8 do
design técnico)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import settings
from .storage.files import get_run, list_runs, read_jsonl
from .storage.test_spec import TestSpec, TestSpecError, discover_test_specs

# O console do Windows por padrão usa a codepage legada (cp1252), que não
# codifica emoji (🐞🔎💡 usados para achados) e derruba o processo inteiro no
# meio de uma execução. UTF-8 é suportado por qualquer terminal usado hoje
# (Windows Terminal, PowerShell 7+, VS Code) — reconfigurar aqui evita o crash.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

app = typer.Typer(
    help="Agente QA — usa Claude para rodar testes manuais de UI num browser real.",
    no_args_is_help=True,
)
runs_app = typer.Typer(help="Consultar execuções passadas.")
app.add_typer(runs_app, name="runs")

console = Console()


@app.command()
def run(
    test_file: Path = typer.Argument(
        ...,
        help="Arquivo YAML de um teste (ex.: testes/exemplo.yaml), ou uma pasta — "
        "roda todos os *.yaml dela, recursivamente, em ordem alfabética.",
    ),
    mode: str = typer.Option(
        "visivel",
        "--mode",
        "-m",
        help="'visivel' abre o browser e mostra o progresso ao vivo; "
        "'background' roda headless, só mostra o resultado no fim.",
    ),
    max_steps: Optional[int] = typer.Option(
        None, "--max-steps", help=f"Padrão: {settings.max_steps} (ou AGENTE_QA_MAX_STEPS)."
    ),
    timeout: Optional[int] = typer.Option(
        None,
        "--timeout",
        help=f"Timeout em segundos. Padrão: {settings.max_duration_seconds} "
        "(ou AGENTE_QA_MAX_DURATION_SECONDS).",
    ),
    tags: Optional[list[str]] = typer.Option(
        None,
        "--tag",
        "-t",
        help="Roda só os testes que tiverem QUALQUER uma destas tags (repita --tag "
        "para mais de uma; requer um campo 'tags:' no YAML do teste). Combina com "
        "um arquivo único ou uma pasta.",
    ),
) -> None:
    """Executa um teste (ou todos os de uma pasta, opcionalmente filtrados por tag)
    do início ao fim e gera o(s) relatório(s)."""
    if mode not in ("visivel", "background"):
        console.print("[red]--mode precisa ser 'visivel' ou 'background'.[/red]")
        raise typer.Exit(code=2)

    try:
        test_specs, spec_errors = discover_test_specs(test_file, tags)
    except TestSpecError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    for bad_path, message in spec_errors:
        console.print(f"[red]{bad_path}: {message}[/red]")

    if not test_specs:
        if not spec_errors:
            console.print("[yellow]Nenhum teste corresponde ao filtro de tag informado.[/yellow]")
        raise typer.Exit(code=2)

    # Import tardio: evita puxar Playwright/claude-agent-sdk em comandos que não
    # precisam (ex.: `agente-qa validate`, `agente-qa --help`).
    from .agent.loop import run_test

    is_batch = len(test_specs) > 1 or bool(spec_errors)
    results: list[tuple[TestSpec, object]] = []

    for spec in test_specs:
        if is_batch:
            console.rule(f"[bold]{spec.label}[/bold] ({spec.path})")
        try:
            result = run_test(
                spec, settings, mode, max_steps=max_steps, timeout_seconds=timeout, console=console
            )
        except RuntimeError as exc:
            # Erro de configuração/autenticação do CLI 'claude' — mensagem já é acionável.
            console.print(f"[red]{exc}[/red]")
            if not is_batch:
                raise typer.Exit(code=2)
            results.append((spec, None))
            continue
        results.append((spec, result))

    if is_batch:
        _print_batch_summary(results, spec_errors)

    any_problem = bool(spec_errors) or any(
        result is None or result.outcome == "falhou" or result.bugs_count > 0
        for _, result in results
    )
    if any_problem:
        raise typer.Exit(code=1)


def _print_batch_summary(
    results: list[tuple[TestSpec, object]], spec_errors: list[tuple[Path, str]]
) -> None:
    table = Table(title="Resumo da execução em lote", show_header=True, header_style="bold")
    table.add_column("Teste")
    table.add_column("Resultado")
    table.add_column("Bugs")
    table.add_column("Relatório")

    for spec, result in results:
        if result is None:
            table.add_row(str(spec.path), "[red]erro (execução)[/red]", "—", "—")
            continue
        color = {"passou": "green", "falhou": "red", "incompleto": "yellow"}.get(result.outcome, "white")
        table.add_row(
            spec.label,
            f"[{color}]{result.outcome}[/{color}]",
            str(result.bugs_count),
            result.report_path,
        )
    for bad_path, _ in spec_errors:
        table.add_row(str(bad_path), "[red]erro (yaml)[/red]", "—", "—")

    console.print(table)


@runs_app.command("list")
def runs_list(
    test_file: Optional[Path] = typer.Argument(
        None, help="Filtra por um arquivo de teste específico. Omitir para ver todas as execuções."
    )
) -> None:
    """Lista execuções passadas (mais recente primeiro)."""
    runs = list_runs(settings.data_dir, str(test_file) if test_file else None)
    if not runs:
        console.print("Nenhuma execução encontrada ainda.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Execução")
    table.add_column("Teste")
    table.add_column("Modo")
    table.add_column("Resultado")
    table.add_column("Bugs")
    table.add_column("Início")

    for r in runs:
        outcome = r.get("outcome") or r.get("status", "?")
        color = {"passou": "green", "falhou": "red", "incompleto": "yellow"}.get(outcome, "white")
        table.add_row(
            r["run_id"],
            r.get("test_label", r.get("test_file", "?")),
            r.get("mode", "?"),
            f"[{color}]{outcome}[/{color}]",
            "—",
            r.get("started_at", "?"),
        )
    console.print(table)


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Id da execução (ver 'agente-qa runs list')."),
    open_file: bool = typer.Option(False, "--open", help="Abrir o relatório no visualizador padrão do SO."),
) -> None:
    """Mostra o caminho do relatório de uma execução (e opcionalmente o abre)."""
    run_data = get_run(settings.data_dir, run_id)
    if run_data is None:
        console.print(f"[red]Execução '{run_id}' não encontrada.[/red]")
        raise typer.Exit(code=2)

    report_path = settings.data_dir / "runs" / run_id / "report.md"
    if not report_path.exists():
        console.print(f"[yellow]Execução encontrada, mas sem relatório em {report_path}.[/yellow]")
        raise typer.Exit(code=1)

    console.print(str(report_path))
    if open_file:
        _open_in_os(report_path)


_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
_LEVEL_COLOR = {"DEBUG": "dim", "INFO": "white", "WARN": "yellow", "ERROR": "red"}


def _format_event_summary(ev: dict) -> str:
    """Reduz o payload (que varia por `type`) a uma linha legível — o JSON bruto
    completo continua disponível via `--json` para quem precisar de todo campo."""
    ev_type = ev.get("type")
    if ev_type == "action":
        tool = ev.get("tool", "?")
        tool_input = {k: v for k, v in (ev.get("input") or {}).items()}
        args = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
        return f"{tool}({args})"
    if ev_type == "tool_error":
        return f"{ev.get('tool', '?')} → {ev.get('error', '?')}"
    if ev_type == "finding":
        finding = ev.get("finding", {})
        sev = f" sev.{finding.get('severity')}" if finding.get("severity") else ""
        return f"[{finding.get('kind', '?')}{sev}] {finding.get('title', '?')}"
    if ev_type == "question":
        return str(ev.get("text", ""))
    if ev_type == "finish":
        return f"{ev.get('outcome', '?')}: {ev.get('summary', '')}"
    if ev_type == "run_start":
        return f"{ev.get('label', '?')} → {ev.get('target_url', '?')} ({ev.get('mode', '?')})"
    if ev_type == "fatal_error":
        return str(ev.get("error", ""))
    if ev_type == "page_errors":
        errors = ev.get("errors") or []
        return f"{len(errors)} erro(s)" if errors else "sem erros"
    if ev_type == "screenshot":
        return str(ev.get("path", ""))
    return str({k: v for k, v in ev.items() if k not in ("ts", "run_id", "step", "level", "type")})


@app.command()
def logs(
    run_id: str = typer.Argument(..., help="Id da execução (ver 'agente-qa runs list')."),
    level: Optional[str] = typer.Option(
        None,
        "--level",
        "-l",
        help="Mostra só eventos deste nível OU MAIS GRAVE (DEBUG < INFO < WARN < ERROR).",
    ),
    type_filter: Optional[str] = typer.Option(
        None,
        "--type",
        help="Mostra só eventos deste tipo (ex.: action, tool_error, finding, finish).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Imprime cada evento como JSON bruto em vez da linha formatada."
    ),
) -> None:
    """Mostra o log estruturado de uma execução (data/runs/<run-id>/events.jsonl),
    formatado e filtrável por nível/tipo — complementa o relatório (`report`),
    que só traz os achados, com o passo a passo completo da execução."""
    events_path = settings.data_dir / "runs" / run_id / "events.jsonl"
    events = read_jsonl(events_path)
    if not events:
        console.print(f"[yellow]Nenhum evento encontrado para a execução '{run_id}'.[/yellow]")
        raise typer.Exit(code=1)

    min_level = _LEVEL_ORDER.get((level or "DEBUG").upper(), 0)
    shown = 0
    for ev in events:
        if _LEVEL_ORDER.get(ev.get("level", "INFO"), 1) < min_level:
            continue
        if type_filter and ev.get("type") != type_filter:
            continue
        shown += 1
        if as_json:
            console.print_json(data=ev)
            continue
        ev_level = ev.get("level", "INFO")
        color = _LEVEL_COLOR.get(ev_level, "white")
        ts = str(ev.get("ts", "")).split("T")[-1].split("+")[0]
        console.print(
            f"[dim]{ts}[/dim] passo {ev.get('step', 0):02d} "
            f"[{color}]{ev_level:<5}[/{color}] [cyan]{ev.get('type', '?')}[/cyan] "
            f"{escape(_format_event_summary(ev))}"
        )

    if shown == 0:
        console.print("[yellow]Nenhum evento corresponde aos filtros informados.[/yellow]")


def _open_in_os(path: Path) -> None:
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform.startswith("win"):
        import os

        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


@app.command()
def validate(
    test_file: Path = typer.Argument(
        ..., help="Arquivo YAML de um teste, ou uma pasta com vários, a validar."
    ),
    tags: Optional[list[str]] = typer.Option(
        None, "--tag", "-t", help="Valida só os testes que tiverem alguma destas tags."
    ),
) -> None:
    """Valida a sintaxe de um ou mais arquivos de teste sem executar nada."""
    try:
        test_specs, spec_errors = discover_test_specs(test_file, tags)
    except TestSpecError as exc:
        console.print(f"[red]Inválido: {exc}[/red]")
        raise typer.Exit(code=2)

    for test_spec in test_specs:
        tags_suffix = f" (tags: {', '.join(test_spec.tags)})" if test_spec.tags else ""
        console.print(
            f"[green]OK[/green] — {test_spec.path}: '{test_spec.label}' → {test_spec.target_url} "
            f"({len(test_spec.steps)} passos){tags_suffix}"
        )

    for bad_path, message in spec_errors:
        console.print(f"[red]Inválido[/red] — {bad_path}: {message}")

    if spec_errors:
        raise typer.Exit(code=2)
    if not test_specs:
        console.print("[yellow]Nenhum teste corresponde ao filtro de tag informado.[/yellow]")


if __name__ == "__main__":
    app()
