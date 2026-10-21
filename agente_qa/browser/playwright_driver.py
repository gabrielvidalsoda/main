"""Camada de automação de browser. Envolve o Playwright (API assíncrona) e expõe
um conjunto pequeno e genérico de ações — nenhuma delas conhece o domínio de
nenhum sistema específico.

É assíncrona porque o loop do agente roda dentro de um event loop asyncio (o
claude-agent-sdk é async), e a API síncrona do Playwright não pode ser usada aí.

Elementos interativos da página são identificados por um atributo injetado
`data-aqa-id`, recalculado a cada chamada de `snapshot()`. Isso evita depender de
seletores CSS frágeis: o agente só precisa saber o número do elemento que viu no
snapshot mais recente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.async_api import Browser, Page, Playwright, async_playwright

# Elementos considerados "interativos" o suficiente para aparecer no snapshot.
_INTERACTIVE_SELECTORS = [
    "a[href]",
    "button",
    "input",
    "select",
    "textarea",
    "[role='button']",
    "[role='link']",
    "[role='checkbox']",
    "[role='radio']",
    "[role='tab']",
    "[role='menuitem']",
    "[role='combobox']",
    "[role='switch']",
    "[role='option']",
    "[onclick]",
    "[contenteditable='true']",
]

# Script injetado na página para localizar elementos interativos visíveis e
# marcá-los com um id sequencial. Mantido como JS puro (roda no contexto da
# página, não do Node/Python) — sem dependências externas.
_SNAPSHOT_JS = """
(selectors) => {
  // Ids de um snapshot anterior ficam presos em elementos que já saíram de tela
  // (menu colapsado, accordion fechado…) — sem isto, um id reciclado no snapshot
  // novo pode colidir com o nó antigo (ainda escondido) em '[data-aqa-id=N]' e o
  // Playwright resolve para o nó errado (invisível) via `.first`.
  document.querySelectorAll('[data-aqa-id]').forEach((el) => el.removeAttribute('data-aqa-id'));
  const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
  const seen = new Set();
  let counter = 0;
  const elements = [];
  for (const el of nodes) {
    if (seen.has(el)) continue;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0
      && style.visibility !== 'hidden' && style.display !== 'none';
    if (!visible) continue;
    counter += 1;
    el.setAttribute('data-aqa-id', String(counter));
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const rawText = el.innerText || el.value || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || el.getAttribute('title') || '';
    const text = rawText.trim().replace(/\\s+/g, ' ').slice(0, 120);
    const type = el.getAttribute('type') || '';
    const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true';
    let checked;
    const isCheckableInput = tag === 'input' && (type === 'checkbox' || type === 'radio');
    const isCheckableRole = ['checkbox', 'radio', 'switch'].includes(role);
    if (isCheckableInput || isCheckableRole) {
      if (typeof el.checked === 'boolean') checked = el.checked;
      else if (el.getAttribute('aria-checked')) checked = el.getAttribute('aria-checked') === 'true';
    }
    const href = el.getAttribute('href') || null;
    elements.push({ id: counter, tag, role, type, text, disabled, checked, href });
  }
  return { url: window.location.href, title: document.title, elements };
}
"""

MAX_ELEMENTS_IN_SNAPSHOT = 80


@dataclass
class PageSnapshot:
    url: str
    title: str
    elements: list[dict[str, Any]]
    truncated: bool = False

    def to_text(self) -> str:
        lines = [f"URL: {self.url}", f"Título: {self.title}", "", "Elementos interativos:"]
        if not self.elements:
            lines.append("(nenhum elemento interativo visível encontrado)")
        for el in self.elements:
            bits = [f"[{el['id']}] <{el['tag']}", f"role={el['role']}"]
            if el.get("type"):
                bits.append(f"type={el['type']}")
            if el.get("disabled"):
                bits.append("disabled")
            if el.get("checked") is not None:
                bits.append(f"checked={el['checked']}")
            if el.get("href"):
                bits.append(f"href={el['href']}")
            header = " ".join(bits) + ">"
            text = el.get("text") or ""
            lines.append(f"{header} \"{text}\"" if text else header)
        if self.truncated:
            lines.append(f"... lista truncada em {MAX_ELEMENTS_IN_SNAPSHOT} elementos.")
        return "\n".join(lines)


class BrowserActionError(RuntimeError):
    """Erro ao executar uma ação no browser — pensado para virar texto de
    tool_result de erro, não para derrubar o loop do agente."""


class BrowserDriver:
    def __init__(self, headless: bool) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.page: Page | None = None
        self._console_errors: list[str] = []
        self._network_errors: list[str] = []

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        context = await self._browser.new_context(viewport={"width": 1366, "height": 900})
        self.page = await context.new_page()
        self.page.on(
            "console",
            lambda msg: self._console_errors.append(msg.text) if msg.type == "error" else None,
        )
        self.page.on(
            "response",
            lambda resp: self._network_errors.append(f"{resp.status} {resp.url}")
            if resp.status >= 400
            else None,
        )
        self.page.on("pageerror", lambda exc: self._console_errors.append(str(exc)))

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()

    # ── navegação ────────────────────────────────────────────────────────────

    # 60s (não 20s): no arranque a frio, o Vite às vezes descobre dependências
    # novas durante o primeiro crawl, re-otimiza-as e força um segundo reload
    # completo da página (`window.location.reload()`) — isso já consumiu ~15s
    # em teste local antes mesmo do "load" final disparar.
    _NAV_TIMEOUT_MS = 60000

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="load", timeout=self._NAV_TIMEOUT_MS)

    async def go_back(self) -> None:
        resp = await self.page.go_back(wait_until="load", timeout=self._NAV_TIMEOUT_MS)
        if resp is None:
            raise BrowserActionError("Não há página anterior no histórico para voltar.")

    async def reload(self) -> None:
        await self.page.reload(wait_until="load", timeout=self._NAV_TIMEOUT_MS)

    # ── snapshot / percepção ────────────────────────────────────────────────

    async def snapshot(self) -> PageSnapshot:
        result = await self.page.evaluate(_SNAPSHOT_JS, _INTERACTIVE_SELECTORS)
        elements = result["elements"]
        truncated = len(elements) > MAX_ELEMENTS_IN_SNAPSHOT
        if truncated:
            elements = elements[:MAX_ELEMENTS_IN_SNAPSHOT]
        return PageSnapshot(
            url=result["url"], title=result["title"], elements=elements, truncated=truncated
        )

    async def screenshot_bytes(self) -> bytes:
        return await self.page.screenshot(type="png")

    def read_page_errors(self) -> list[str]:
        errors = self._console_errors + self._network_errors
        self._console_errors = []
        self._network_errors = []
        return errors

    # ── ações ────────────────────────────────────────────────────────────────

    async def _locator(self, element_id: int):
        loc = self.page.locator(f"[data-aqa-id='{element_id}']")
        if await loc.count() == 0:
            raise BrowserActionError(
                f"Elemento [{element_id}] não existe mais na página — peça um snapshot novo "
                "antes de tentar de novo."
            )
        return loc.first

    async def click(self, element_id: int) -> None:
        loc = await self._locator(element_id)
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.click(timeout=5000)
        # Menus/accordions com transição CSS (ex.: expand/collapse) ainda estão
        # se movendo logo após o clique — um clique seguinte antes disso pode
        # acertar o elemento errado ou achar tudo "not visible".
        await self.page.wait_for_timeout(400)

    async def type(self, element_id: int, text: str) -> None:
        loc = await self._locator(element_id)
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.fill(text, timeout=5000)

    async def select_option(self, element_id: int, value: str) -> None:
        loc = await self._locator(element_id)
        try:
            await loc.select_option(value=value, timeout=5000)
        except Exception:
            await loc.select_option(label=value, timeout=5000)

    async def press_key(self, key: str) -> None:
        await self.page.keyboard.press(key)

    async def scroll(self, direction: str, element_id: int | None = None) -> None:
        dy = 350 if direction == "down" else -350
        if element_id is not None:
            loc = await self._locator(element_id)
            await loc.evaluate("(el, dy) => el.scrollBy(0, dy)", dy)
        else:
            await self.page.mouse.wheel(0, dy)

    async def wait_for(self, text: str | None, timeout_ms: int) -> None:
        timeout_ms = min(timeout_ms, 15000)
        if text:
            await self.page.get_by_text(text, exact=False).first.wait_for(
                state="visible", timeout=timeout_ms
            )
        else:
            await self.page.wait_for_timeout(timeout_ms)
