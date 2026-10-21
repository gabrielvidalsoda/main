"""O loop do agente (§4 do design técnico): decide, executa, observa, repete —
até chamar `finish` ou atingir o limite de passos/tempo.

Aqui o loop em si é do claude-agent-sdk: `query()` roda o CLI `claude` (com a
autenticação da assinatura, sem API key), chama nossas tools MCP in-process e
transmite as mensagens de volta. Nós só traduzimos esse stream para a saída no
terminal e cortamos por tempo/`finish`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultError,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from rich.console import Console

from ..browser.playwright_driver import BrowserDriver
from ..config import Settings
from ..storage.files import RunPaths, create_run, finalize_run, read_json
from ..storage.test_spec import TestSpec
from ..report.render import render_report
from ..terminal.live_view import (
    print_background_progress,
    print_end,
    print_start,
    print_step,
)
from .prompt import load_system_prompt
from .tools import (
    ALLOWED_TOOLS,
    SERVER_NAME,
    TOOL_PREFIX,
    RunContext,
    build_qa_server,
    level_for_outcome,
    log_event,
)

# Tools nativas do Claude Code que não fazem sentido aqui — o agente só deve usar
# as tools de browser/QA. Bloqueadas explicitamente, e o can_use_tool abaixo nega
# qualquer outra que apareça no futuro.
_BUILTIN_TOOLS = [
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit",
    "NotebookEdit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "Task",
    "ExitPlanMode", "SlashCommand",
]

_CLI_HELP = (
    "O agente usa o claude-agent-sdk, que precisa do Claude Code CLI logado com a "
    "sua assinatura:\n"
    "  npm install -g @anthropic-ai/claude-code\n"
    "  claude        # faça login uma vez\n"
    "Depois disso, rode o teste de novo."
)


@dataclass
class RunResult:
    run_id: str
    outcome: str
    report_path: str
    findings_count: int
    bugs_count: int


def _build_intro_message(test_spec: TestSpec, snapshot_text: str) -> str:
    steps_block = "\n".join(f"{i}. {s}" for i, s in enumerate(test_spec.steps, start=1))
    return (
        f"Teste: {test_spec.label}\n\n"
        f"Passos:\n{steps_block}\n\n"
        f"Critério de avaliação:\n{test_spec.evaluation}\n\n"
        f"Estado inicial da página (já navegada para {test_spec.target_url}):\n{snapshot_text}"
    )


def _relay_message(ctx: RunContext, message: object, max_steps: int) -> None:
    """Traduz uma mensagem do stream do SDK para a saída no terminal."""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                name = block.name
                if name.startswith(TOOL_PREFIX):
                    name = name[len(TOOL_PREFIX):]
                if ctx.mode == "visivel":
                    # ctx.step é incrementado pelo handler da tool logo a seguir;
                    # +1 é o número que esta chamada vai ter.
                    print_step(ctx.console, ctx.step + 1, name, block.input or {}, "")
                else:
                    print_background_progress(ctx.console, ctx.step + 1, max_steps)
            elif isinstance(block, TextBlock) and ctx.mode == "visivel":
                text = block.text.strip()
                if text:
                    ctx.console.print(f"[dim]{text}[/dim]")
    elif isinstance(message, UserMessage):
        for block in getattr(message, "content", None) or []:
            is_error = getattr(block, "is_error", False)
            if is_error and ctx.mode == "visivel":
                content = getattr(block, "content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                ctx.console.print(f"  [red]{str(content).strip()}[/red]")


async def _drive_agent(
    ctx: RunContext,
    test_spec: TestSpec,
    settings: Settings,
    system_prompt: str,
    max_steps: int,
    timeout_seconds: int,
) -> None:
    driver = ctx.driver
    await driver.start()
    try:
        await driver.goto(test_spec.target_url)
        snapshot_text = (await driver.snapshot()).to_text()
        intro = _build_intro_message(test_spec, snapshot_text)

        server = build_qa_server(ctx)

        # can_use_tool é o único portão: libera as tools de QA (mcp__qa__*) e nega
        # qualquer outra. Sem `allowed_tools` de propósito — uma entrada lá
        # auto-aprovaria a tool antes do callback e o SDK ainda emitiria um aviso.
        allowed = set(ALLOWED_TOOLS)

        async def can_use_tool(tool_name: str, tool_input: dict, context: object):
            if tool_name in allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(
                message="Só as tools de QA (mcp__qa__*) estão liberadas neste agente."
            )

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={SERVER_NAME: server},
            disallowed_tools=_BUILTIN_TOOLS,
            can_use_tool=can_use_tool,
            permission_mode="default",
            model=settings.model,
            max_turns=max_steps,
            setting_sources=[],
            # Screenshots em base64 podem passar do limite padrão de 1MB por
            # mensagem stdout do SDK; sem isto o teste morre no primeiro
            # screenshot "pesado" (ex.: página longa/com muitas imagens).
            max_buffer_size=20 * 1024 * 1024,
        )

        start_time = time.time()
        stream = query(prompt=intro, options=options)
        try:
            async for message in stream:
                _relay_message(ctx, message, max_steps)
                if ctx.finished:
                    break
                if (time.time() - start_time) > timeout_seconds:
                    ctx.summary = ctx.summary or (
                        f"Tempo limite ({timeout_seconds}s) atingido antes do agente concluir."
                    )
                    break
        except CLINotFoundError as exc:
            raise RuntimeError(f"CLI 'claude' não encontrado.\n\n{_CLI_HELP}") from exc
        except ResultError as exc:
            if getattr(exc, "subtype", None) == "error_max_turns":
                ctx.summary = ctx.summary or f"Limite de passos ({max_steps}) atingido."
            else:
                raise RuntimeError(
                    f"O CLI 'claude' encerrou com erro:\n{exc}\n\n"
                    f"Se for erro de autenticação, rode `claude` e faça login.\n{_CLI_HELP}"
                ) from exc
        except ProcessError as exc:
            raise RuntimeError(
                f"O CLI 'claude' falhou:\n{exc}\n\n"
                f"Se for erro de autenticação, rode `claude` e faça login.\n{_CLI_HELP}"
            ) from exc
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
    finally:
        await driver.close()


def run_test(
    test_spec: TestSpec,
    settings: Settings,
    mode: str,
    max_steps: int | None = None,
    timeout_seconds: int | None = None,
    console: Console | None = None,
) -> RunResult:
    console = console or Console()
    max_steps = max_steps or settings.max_steps
    timeout_seconds = timeout_seconds or settings.max_duration_seconds
    headless = mode == "background"

    system_prompt, prompt_hash = load_system_prompt(settings.prompt_path)
    paths: RunPaths = create_run(settings.data_dir, test_spec, mode, prompt_hash)

    ctx = RunContext(
        driver=BrowserDriver(headless=headless),
        paths=paths,
        mode=mode,
        console=console,
    )

    print_start(console, test_spec.label, test_spec.target_url, mode, paths.run_id)
    log_event(
        ctx,
        "run_start",
        {"label": test_spec.label, "target_url": test_spec.target_url, "mode": mode},
    )

    try:
        try:
            asyncio.run(
                _drive_agent(ctx, test_spec, settings, system_prompt, max_steps, timeout_seconds)
            )
        except KeyboardInterrupt:
            ctx.finished = True
            ctx.outcome = "incompleto"
            ctx.summary = ctx.summary or "Interrompido pelo usuário (Ctrl+C)."
    except RuntimeError as exc:
        # Erro fatal (CLI 'claude' ausente/não autenticado, etc.) — registrado
        # antes de propagar, senão o log fica mudo sobre por que a execução
        # nunca chegou a terminar.
        log_event(ctx, "fatal_error", {"error": str(exc)}, level="ERROR")
        raise

    if not ctx.finished:
        # Limite de passos/tempo atingido sem o modelo chamar finish() — a tool
        # nunca roda, então esse desfecho não fica registado em events.jsonl a
        # menos que a gente registe aqui também.
        ctx.outcome = "incompleto"
        ctx.summary = ctx.summary or (
            f"Limite de passos ({max_steps}) ou de tempo ({timeout_seconds}s) atingido "
            "antes do agente concluir o teste."
        )
        log_event(
            ctx,
            "finish",
            {"outcome": ctx.outcome, "summary": ctx.summary},
            level=level_for_outcome(ctx.outcome),
        )

    finalize_run(paths, ctx.outcome, ctx.summary, ctx.step)
    started_at = read_json(paths.run_json)["started_at"]

    report_path = render_report(
        paths=paths,
        test_spec=test_spec,
        run_id=paths.run_id,
        mode=mode,
        prompt_hash=prompt_hash,
        started_at=started_at,
        outcome=ctx.outcome,
        summary=ctx.summary,
        steps_executed=ctx.step,
        findings=ctx.findings,
        questions=ctx.questions,
    )

    bugs = [f for f in ctx.findings if f.kind == "bug"]
    observations = [f for f in ctx.findings if f.kind == "observacao"]
    suggestions = [f for f in ctx.findings if f.kind == "sugestao"]

    print_end(
        console,
        ctx.outcome,
        ctx.summary,
        n_bugs=len(bugs),
        n_obs=len(observations),
        n_sug=len(suggestions),
        report_path=report_path,
    )

    return RunResult(
        run_id=paths.run_id,
        outcome=ctx.outcome,
        report_path=report_path,
        findings_count=len(ctx.findings),
        bugs_count=len(bugs),
    )
