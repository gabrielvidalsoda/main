# Agente QA

> Claude-agent-driven UI QA agent with Playwright MCP.

An agentic QA tool that parses YAML test specifications, navigates the UI via Playwright MCP, executes checks, and produces structured results with a rich terminal UI.

## Features

- **YAML test specs** — define tests in simple YAML (	estes/exemplo.yaml, 	estes/teste-basico-ui.yaml).
- **Playwright MCP integration** — browser automation through the Model Context Protocol.
- **Rich terminal UI** — live progress with ich.
- **Pytest browser layer** — optional pytest integration for browser tests.
- **Extensible** — agent workflow powered by claude-agent-sdk.

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/claude/docs/claude-code) configured
- Playwright MCP available

## Quick start

`ash
pip install -r requirements.txt
playwright install
cp .env.example .env  # configure as needed
python -m agente_qa run testes/exemplo.yaml
`

## Structure

`
├── agente_qa/   # Core package
├── testes/      # Example YAML specs
├── docs/        # Design docs
└── pyproject.toml
`

## License

[MIT](LICENSE) © Gabriel Vidal