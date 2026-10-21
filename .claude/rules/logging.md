# Logging estruturado — convenção deste projeto

Padrão destilado de três referências gerais sobre logging/observability (em
`Referências e exemplos/Logs/` na raiz do workspace) e adaptado ao que faz
sentido para uma **ferramenta local, de processo único, sem servidor** — nada
de agregação centralizada (ELK/Datadog/Splunk), correlação entre serviços, ou
infraestrutura de alerta: isso resolveria um problema que este projeto não
tem. O que se aproveita das referências é: logs estruturados (não texto
solto), níveis de severidade consistentes, nunca logar segredos, e documentar
o formato — o resto (retenção agressiva, alertas em tempo real, ferramentas de
agregação) é overkill aqui.

## Onde vive o log de uma execução

Cada `agente-qa run` já grava um log estruturado em
`data/runs/<run-id>/events.jsonl` (uma linha JSON por evento, formato JSONL) —
implementado em `agente_qa/agent/tools.py` (`log_event`) e usado tanto pelas
tools do agente quanto por `agente_qa/agent/loop.py` para os eventos de nível
de execução (`run_start`, `finish`, `fatal_error`).

Isto é **diferente** de `report.md` (o relatório final, só com os achados —
para o QA humano ler) e de `run.json` (metadados/resultado da execução, para
`runs list`). `events.jsonl` é o rastro passo a passo — para depurar *por que*
o agente fez o que fez, não só *o que* encontrou.

## Formato de cada evento

Todo evento é um objeto JSON com estes campos sempre presentes:

```json
{"ts": "2026-09-23T10:00:01+00:00", "run_id": "...", "step": 1, "level": "INFO", "type": "action", "...": "..."}
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `ts` | sim | timestamp ISO-8601 em UTC (`utc_now_iso()`) |
| `run_id` | sim | id da execução — repetido em toda linha (não só implícito na pasta) para o dia em que precisarmos concatenar/grepar logs de mais de uma execução |
| `step` | sim | número do passo do agente no momento do evento (mesmo contador do `report.md`) |
| `level` | sim | `DEBUG` \| `INFO` \| `WARN` \| `ERROR` — ver mapeamento abaixo |
| `type` | sim | tipo do evento: `run_start`, `action`, `tool_error`, `screenshot`, `page_errors`, `finding`, `question`, `finish`, `fatal_error` |
| … | varia | campos específicos do `type` (ex.: `action` tem `tool`+`input`; `tool_error` tem `tool`+`input`+`error`) |

**Sempre grave um evento por ação significativa já consolidada** (ex.: uma
chamada de tool = um evento, com toda a informação relevante dentro), nunca
fragmente uma ação em várias linhas — é o mesmo princípio de "canonical log
line"/"wide event" das referências, só que aqui a unidade natural já é a
chamada de tool.

## Níveis — quando usar cada um

| Nível | Quando |
|---|---|
| `DEBUG` | Detalhe que não importa numa leitura normal (ex.: `screenshot` manual, `page_errors` sem erro nenhum) |
| `INFO` | Fluxo normal: `action` bem-sucedida, `run_start`, `question`, `finding` de observação/sugestão, `finish` com `outcome: "passou"` |
| `WARN` | Algo a olhar mas não necessariamente errado: `page_errors` com erros de console/rede, `finding` de bug severidade 2, `finish` com `outcome: "incompleto"` |
| `ERROR` | Falha real: `tool_error`, `finding` de bug severidade 1, `finish` com `outcome: "falhou"`, `fatal_error` (CLI `claude` ausente/não autenticado etc.) |

O mapeamento de desfecho→nível vive em `level_for_outcome()`
(`agente_qa/agent/tools.py`) — reutilize essa função em vez de duplicar o
dicionário `{"falhou": "ERROR", "incompleto": "WARN"}` em outro lugar.

## Nunca logar segredos

O `README.md` do projeto já documenta que, na v1, quem escreve um teste pode
copiar uma credencial real para dentro de um passo em `steps:` (não há leitura
de variável de ambiente pelo agente ainda). Por isso, a tool `type` — a única
que recebe texto livre digitado pelo agente — **nunca** grava o valor de
`text` em claro no log: `_redact_input()` substitui por
`"<redacted:N chars>"`. Qualquer tool nova que passe a aceitar texto livre
(senhas, tokens, etc.) deve entrar em `_FREE_TEXT_INPUT_FIELDS` antes de logar.

## Como adicionar um novo evento

Sempre pelo helper, nunca `append_jsonl` direto nem `print`/`console.print`
para dado estruturado:

```python
log_event(ctx, "meu_evento", {"campo": valor}, level="WARN")  # level é opcional, tem default por type
```

Se o `type` for novo, adicione uma entrada em `_DEFAULT_LEVEL`
(`agente_qa/agent/tools.py`) e em `_format_event_summary()`
(`agente_qa/cli.py`) — senão o comando `logs` cai no fallback genérico
(dump do dict) para esse tipo.

## Como ler o log de uma execução

`events.jsonl` é JSONL puro — dá para `grep`/`jq` direto — mas o jeito
recomendado é o comando dedicado, que já formata e filtra:

```bash
uv run agente-qa logs <run-id>                  # tudo, formatado e colorido por nível
uv run agente-qa logs <run-id> --level warn     # WARN e ERROR (nível mínimo)
uv run agente-qa logs <run-id> --type tool_error
uv run agente-qa logs <run-id> --json           # linha a linha, JSON bruto (para grep/jq)
```

## Retenção

Sem rotação/retenção automática — `data/` é local e git-ignorado, cada
execução é uma pasta independente. Se `data/runs/` crescer demais, apagar
pastas de execuções antigas manualmente é seguro (não há índice central para
atualizar). Não vale a pena construir rotação automática para uma ferramenta
de uso individual — reavaliar só se `data/runs/` passar a ser compartilhado
entre várias pessoas/CI.
