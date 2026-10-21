"""As ferramentas (tools) expostas a Claude, como um servidor MCP in-process do
claude-agent-sdk.

Nenhuma tool conhece o domínio de nenhum sistema — são só as ações genéricas que
qualquer QA manual faria num browser (§3 do design técnico). `report_finding` e
`note_question` são as duas exceções "não-browser": são como o agente fala de volta
com o relatório. `finish` encerra o teste com um veredito estruturado.

As tools são construídas por `build_qa_server(ctx)`, que fecha sobre o
`RunContext` da execução atual — assim os handlers mutam os findings/o estado sem
depender de variável global.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from rich.console import Console

from ..browser.playwright_driver import BrowserActionError, BrowserDriver
from ..storage.files import RunPaths, append_jsonl, utc_now_iso

SERVER_NAME = "qa"

# Prefixo com que o claude-agent-sdk expõe estas tools ao modelo
# (ex.: a tool "finish" vira "mcp__qa__finish").
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"

_TOOL_NAMES = [
    "goto",
    "click",
    "type",
    "select_option",
    "press_key",
    "scroll",
    "go_back",
    "reload",
    "wait_for",
    "screenshot",
    "read_page_errors",
    "report_finding",
    "note_question",
    "finish",
]

ALLOWED_TOOLS = [TOOL_PREFIX + name for name in _TOOL_NAMES]


@dataclass
class Finding:
    index: int
    kind: str  # bug | observacao | sugestao
    severity: int | None
    title: str
    expected: str
    observed: str
    steps_to_reproduce: list[str]
    screenshot_path: str | None
    step: int


@dataclass
class RunContext:
    driver: BrowserDriver
    paths: RunPaths
    mode: str
    console: Console
    step: int = 0
    findings: list[Finding] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    finished: bool = False
    outcome: str = "incompleto"
    summary: str = ""

    async def _save_screenshot(self, name: str) -> str:
        png = await self.driver.screenshot_bytes()
        rel_path = f"screenshots/{name}.png"
        (self.paths.run_dir / rel_path).write_bytes(png)
        return rel_path


# Nível padrão por tipo de evento — pode ser sobrescrito por chamada (ex.: um
# "finding" de severidade 1 vira ERROR, não WARN). Ver .claude/rules/logging.md
# (convenção de logging deste projeto) para o raciocínio completo.
_DEFAULT_LEVEL = {
    "action": "INFO",
    "tool_error": "ERROR",
    "screenshot": "DEBUG",
    "page_errors": "WARN",
    "finding": "WARN",
    "question": "INFO",
    "finish": "INFO",
}

# Tools cujo input pode conter texto livre digitado pelo usuário/agente — e
# portanto, potencialmente, um segredo (o README documenta que, na v1, quem
# escreve o teste pode copiar uma credencial real para dentro do passo). Nunca
# gravamos esse texto em claro no log estruturado.
_FREE_TEXT_INPUT_FIELDS = {"type": "text"}


def level_for_outcome(outcome: str) -> str:
    """Nível de log para o desfecho de uma execução — usado tanto pela tool
    `finish` quanto por `agent/loop.py` no caminho de timeout/limite de passos,
    que também termina uma execução mas sem passar pela tool."""
    return {"falhou": "ERROR", "incompleto": "WARN"}.get(outcome, "INFO")


def _redact_input(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    field = _FREE_TEXT_INPUT_FIELDS.get(tool_name)
    if not field or field not in args:
        return args
    redacted = dict(args)
    value = str(redacted[field])
    redacted[field] = f"<redacted:{len(value)} chars>"
    return redacted


def log_event(
    ctx: RunContext, event_type: str, payload: dict[str, Any], level: str | None = None
) -> None:
    append_jsonl(
        ctx.paths.events_jsonl,
        {
            "ts": utc_now_iso(),
            "run_id": ctx.paths.run_id,
            "step": ctx.step,
            "level": level or _DEFAULT_LEVEL.get(event_type, "INFO"),
            "type": event_type,
            **payload,
        },
    )


def _ok(text: str, *extra: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}, *extra]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


# Tools que devem devolver o snapshot novo da página junto do resultado — para o
# modelo "enxergar" o efeito da ação sem ter que pedir um snapshot à parte.
_SNAPSHOT_AFTER = {
    "goto",
    "click",
    "type",
    "select_option",
    "press_key",
    "scroll",
    "go_back",
    "reload",
    "wait_for",
}


def build_qa_server(ctx: RunContext):
    """Monta o servidor MCP in-process com todas as tools, fechando sobre `ctx`."""
    driver = ctx.driver

    async def _with_snapshot(text: str) -> dict[str, Any]:
        snap = (await driver.snapshot()).to_text()
        return _ok(f"{text}\n\nEstado atual da página:\n{snap}")

    @tool("goto", "Navega para uma URL. Use para abrir a URL inicial do teste ou "
          "para corrigir o rumo se a navegação foi parar num lugar errado.",
          {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    async def goto(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.goto(args["url"])
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "goto", "input": _redact_input("goto", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "goto", "input": _redact_input("goto", args)})
        return await _with_snapshot(f"Navegou para {args['url']}.")

    @tool("click", "Clica num elemento pelo id mostrado no snapshot mais recente.",
          {"type": "object", "properties": {"element_id": {"type": "integer"}},
           "required": ["element_id"]})
    async def click(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.click(int(args["element_id"]))
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "click", "input": _redact_input("click", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "click", "input": _redact_input("click", args)})
        return await _with_snapshot(f"Clicou no elemento [{args['element_id']}].")

    @tool("type", "Preenche um campo de texto/textarea pelo id, substituindo o "
          "conteúdo atual.",
          {"type": "object", "properties": {"element_id": {"type": "integer"},
                                            "text": {"type": "string"}},
           "required": ["element_id", "text"]})
    async def type_(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.type(int(args["element_id"]), args["text"])
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "type", "input": _redact_input("type", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "type", "input": _redact_input("type", args)})
        return await _with_snapshot(f"Preencheu o elemento [{args['element_id']}] com o texto dado.")

    @tool("select_option", "Seleciona uma opção num <select> nativo pelo id do "
          "elemento. Para dropdowns customizados (div/lista), use click nas opções "
          "depois de abrir o dropdown.",
          {"type": "object", "properties": {"element_id": {"type": "integer"},
                                            "value": {"type": "string"}},
           "required": ["element_id", "value"]})
    async def select_option(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.select_option(int(args["element_id"]), args["value"])
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "select_option", "input": _redact_input("select_option", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "select_option", "input": _redact_input("select_option", args)})
        return await _with_snapshot(
            f"Selecionou '{args['value']}' no elemento [{args['element_id']}]."
        )

    @tool("press_key", "Envia uma tecla para o elemento focado (ex.: 'Enter', "
          "'Escape', 'Tab').",
          {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]})
    async def press_key(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.press_key(args["key"])
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "press_key", "input": _redact_input("press_key", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "press_key", "input": _redact_input("press_key", args)})
        return await _with_snapshot(f"Pressionou a tecla '{args['key']}'.")

    @tool("scroll", "Rola a página inteira, ou um elemento específico (ex.: uma "
          "grade com scroll próprio), para cima ou para baixo.",
          {"type": "object",
           "properties": {"direction": {"type": "string", "enum": ["up", "down"]},
                          "element_id": {"type": "integer"}},
           "required": ["direction"]})
    async def scroll(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.scroll(args["direction"], args.get("element_id"))
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "scroll", "input": _redact_input("scroll", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "scroll", "input": _redact_input("scroll", args)})
        return await _with_snapshot(f"Rolou {args['direction']}.")

    @tool("go_back", "Volta para a página anterior no histórico do browser "
          "(equivale ao botão Voltar).",
          {"type": "object", "properties": {}, "required": []})
    async def go_back(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.go_back()
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "go_back", "input": _redact_input("go_back", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "go_back", "input": _redact_input("go_back", args)})
        return await _with_snapshot("Voltou para a página anterior.")

    @tool("reload", "Recarrega a página atual (equivale a apertar F5).",
          {"type": "object", "properties": {}, "required": []})
    async def reload(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.reload()
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "reload", "input": _redact_input("reload", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "reload", "input": _redact_input("reload", args)})
        return await _with_snapshot("Recarregou a página.")

    @tool("wait_for", "Espera até um texto aparecer na tela (ex.: uma mensagem de "
          "sucesso), ou espera um tempo fixo se nenhum texto for dado.",
          {"type": "object",
           "properties": {"text": {"type": "string"},
                          "timeout_ms": {"type": "integer"}},
           "required": []})
    async def wait_for(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            await driver.wait_for(args.get("text"), args.get("timeout_ms", 3000))
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "wait_for", "input": _redact_input("wait_for", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        log_event(ctx, "action", {"tool": "wait_for", "input": _redact_input("wait_for", args)})
        return await _with_snapshot("Espera concluída.")

    @tool("screenshot", "Tira um screenshot da tela atual para você INSPECIONAR "
          "visualmente (layout, alinhamento, sobreposição, cores). Não use isto "
          "para gerar evidência de um achado: report_finding já faz isso.",
          {"type": "object", "properties": {}, "required": []})
    async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
        ctx.step += 1
        try:
            rel_path = await ctx._save_screenshot(f"step-{ctx.step:02d}-manual")
        except BrowserActionError as exc:
            log_event(ctx, "tool_error", {"tool": "screenshot", "input": _redact_input("screenshot", args), "error": str(exc)})
            return _err(f"Erro: {exc}")
        png_bytes = (ctx.paths.run_dir / rel_path).read_bytes()
        log_event(ctx, "screenshot", {"path": rel_path})
        return _ok(
            f"Screenshot capturado ({rel_path}).",
            {
                "type": "image",
                "data": base64.b64encode(png_bytes).decode("ascii"),
                "mimeType": "image/png",
            },
        )

    @tool("read_page_errors", "Devolve os erros de Console e as respostas HTTP com "
          "status >= 400 capturados desde a última chamada. O buffer é limpo a cada "
          "chamada.",
          {"type": "object", "properties": {}, "required": []})
    async def read_page_errors(args: dict[str, Any]) -> dict[str, Any]:
        errors = driver.read_page_errors()
        log_event(
            ctx, "page_errors", {"errors": errors}, level="WARN" if errors else "DEBUG"
        )
        if not errors:
            return _ok("Nenhum erro de console/rede desde a última checagem.")
        return _ok("Erros encontrados:\n" + "\n".join(f"- {e}" for e in errors))

    @tool("report_finding", "Registra um achado (bug, observação ou sugestão), com "
          "screenshot de evidência automático. Use sempre que perceber algo que um "
          "usuário real notaria como errado, inconsistente, ou uma boa ideia de melhoria.",
          {"type": "object",
           "properties": {
               "kind": {"type": "string", "enum": ["bug", "observacao", "sugestao"]},
               "severity": {"type": "integer", "enum": [1, 2, 3]},
               "title": {"type": "string"},
               "expected": {"type": "string"},
               "observed": {"type": "string"},
               "steps_to_reproduce": {"type": "array", "items": {"type": "string"}},
           },
           "required": ["kind", "title", "expected", "observed", "steps_to_reproduce"]})
    async def report_finding(args: dict[str, Any]) -> dict[str, Any]:
        idx = len(ctx.findings) + 1
        try:
            rel_path = await ctx._save_screenshot(f"step-{ctx.step:02d}-finding-{idx}")
        except BrowserActionError:
            rel_path = None
        finding = Finding(
            index=idx,
            kind=args["kind"],
            severity=args.get("severity"),
            title=args["title"],
            expected=args["expected"],
            observed=args["observed"],
            steps_to_reproduce=list(args.get("steps_to_reproduce", [])),
            screenshot_path=rel_path,
            step=ctx.step,
        )
        ctx.findings.append(finding)
        # Só "bug" tem severidade; 1 (crítico) vira ERROR, 2 WARN, 3/observação/
        # sugestão ficam INFO — o volume de bugs sérios deve saltar aos olhos
        # numa leitura por nível, sem precisar abrir o report.md.
        finding_level = {1: "ERROR", 2: "WARN"}.get(finding.severity, "INFO")
        log_event(ctx, "finding", {"finding": args, "screenshot": rel_path}, level=finding_level)
        if ctx.mode == "visivel":
            _print_finding(ctx.console, finding)
        return _ok(f"Achado #{idx} registado ({finding.kind}, evidência em {rel_path}).")

    @tool("note_question", "Registra uma dúvida de regra de negócio ou algo que "
          "precisa de confirmação humana — não é um bug, vai para a seção de dúvidas "
          "do relatório.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
    async def note_question(args: dict[str, Any]) -> dict[str, Any]:
        ctx.questions.append(args["text"])
        log_event(ctx, "question", {"text": args["text"]})
        return _ok("Dúvida registada.")

    @tool("finish", "Encerra o teste. Chame só depois de ter tentado todos os passos.",
          {"type": "object",
           "properties": {
               "outcome": {"type": "string", "enum": ["passou", "falhou", "incompleto"]},
               "summary": {"type": "string"},
           },
           "required": ["outcome", "summary"]})
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        ctx.finished = True
        ctx.outcome = args["outcome"]
        ctx.summary = args["summary"]
        log_event(
            ctx,
            "finish",
            {"outcome": ctx.outcome, "summary": ctx.summary},
            level=level_for_outcome(ctx.outcome),
        )
        return _ok("Teste finalizado.")

    tools = [
        goto, click, type_, select_option, press_key, scroll, go_back, reload,
        wait_for, screenshot, read_page_errors, report_finding, note_question, finish,
    ]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def _print_finding(console: Console, finding: Finding) -> None:
    from rich.panel import Panel

    icon = {"bug": "🐞", "observacao": "🔎", "sugestao": "💡"}.get(finding.kind, "•")
    sev = f" · severidade {finding.severity}" if finding.severity else ""
    console.print(
        Panel(
            f"[bold]{finding.title}[/bold]\n"
            f"Esperado: {finding.expected}\n"
            f"Observado: {finding.observed}\n"
            f"Evidência: {finding.screenshot_path}",
            title=f"{icon} {finding.kind}{sev}",
            border_style="red" if finding.kind == "bug" else "yellow",
        )
    )
