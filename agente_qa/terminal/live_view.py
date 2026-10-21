"""Formatação da saída no terminal para os dois modos de execução (§2 do design
técnico). No modo visível, cada passo vira uma linha; no modo background, só um
indicador de progresso simples."""

from __future__ import annotations

from typing import Any

from rich.console import Console


def print_start(console: Console, label: str, target_url: str, mode: str, run_id: str) -> None:
    console.rule(f"[bold]Agente QA[/bold] — {label}")
    console.print(f"Alvo: {target_url}")
    console.print(f"Modo: {mode} · Execução: {run_id}")
    console.print()


def print_step(console: Console, step: int, tool_name: str, tool_input: dict[str, Any], result_text: str) -> None:
    args = ", ".join(f"{k}={v!r}" for k, v in tool_input.items() if k not in ("kind",))
    console.print(f"[dim]passo {step:02d}[/dim] [cyan]{tool_name}[/cyan]({args})")
    if result_text.startswith("Erro"):
        console.print(f"  [red]{result_text}[/red]")


def print_background_progress(console: Console, step: int, max_steps: int) -> None:
    console.print(f"[dim]... passo {step}/{max_steps}[/dim]", end="\r")


def print_end(console: Console, outcome: str, summary: str, n_bugs: int, n_obs: int, n_sug: int, report_path: str) -> None:
    console.print()
    color = {"passou": "green", "falhou": "red", "incompleto": "yellow"}.get(outcome, "white")
    console.rule(f"[bold {color}]{outcome.upper()}[/bold {color}]")
    console.print(summary)
    console.print(f"Bugs: {n_bugs} · Observações: {n_obs} · Sugestões: {n_sug}")
    console.print(f"Relatório: [bold]{report_path}[/bold]")
