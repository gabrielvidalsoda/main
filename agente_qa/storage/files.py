"""Persistência em arquivo — sem banco de dados. Todo estado de uma execução vive em
data/runs/<run-id>/. Escritas de JSON são atômicas (arquivo temporário + rename) para
não corromper nada se o processo for interrompido no meio (Ctrl+C)."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .test_spec import TestSpec


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id(test_slug: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{stamp}-{test_slug}-{suffix}"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


@dataclass
class RunPaths:
    data_dir: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "runs" / self.run_id

    @property
    def run_json(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def events_jsonl(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def report_md(self) -> Path:
        return self.run_dir / "report.md"

    @property
    def screenshots_dir(self) -> Path:
        return self.run_dir / "screenshots"


def create_run(data_dir: Path, test_spec: TestSpec, mode: str, prompt_hash: str) -> RunPaths:
    run_id = new_run_id(test_spec.slug)
    paths = RunPaths(data_dir=data_dir, run_id=run_id)
    paths.screenshots_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        paths.run_json,
        {
            "run_id": run_id,
            "test_file": str(test_spec.path),
            "test_label": test_spec.label,
            "target_url": test_spec.target_url,
            "mode": mode,
            "status": "em_andamento",
            "outcome": None,
            "summary": None,
            "prompt_hash": prompt_hash,
            "started_at": utc_now_iso(),
            "finished_at": None,
            "steps_executed": 0,
        },
    )
    return paths


def finalize_run(
    paths: RunPaths,
    outcome: str,
    summary: str,
    steps_executed: int,
) -> None:
    data = read_json(paths.run_json)
    data.update(
        {
            "status": "concluido",
            "outcome": outcome,
            "summary": summary,
            "finished_at": utc_now_iso(),
            "steps_executed": steps_executed,
        }
    )
    atomic_write_json(paths.run_json, data)


def list_runs(data_dir: Path, test_file: str | None = None) -> list[dict[str, Any]]:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        run_json = run_dir / "run.json"
        if not run_json.exists():
            continue
        try:
            data = read_json(run_json)
        except (json.JSONDecodeError, OSError):
            continue
        if test_file and data.get("test_file") != test_file:
            continue
        runs.append(data)
    return runs


def get_run(data_dir: Path, run_id: str) -> dict[str, Any] | None:
    run_json = data_dir / "runs" / run_id / "run.json"
    if not run_json.exists():
        return None
    return read_json(run_json)
