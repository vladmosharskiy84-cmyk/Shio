"""Файлы развёртывания: Dockerfile, docker-compose.yml, .env.example, deploy.sh, .dockerignore."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_compose_engine_service():
    data = yaml.safe_load(_read("docker-compose.yml"))
    svc = data["services"]["engine"]
    assert svc["build"] == "."
    assert svc["restart"] == "always"
    env = svc["env_file"]
    assert env == ".env" or env == [".env"]
    assert svc["stop_grace_period"] == "60s"
    assert "./data:/app/data" in svc["volumes"]
    assert "./config.yaml:/app/config.yaml:ro" in svc["volumes"]
    assert svc["logging"]["driver"] == "json-file"
    assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    assert svc["mem_limit"] == "1g"


def test_dockerfile():
    text = _read("Dockerfile")
    assert re.search(r"^FROM python:3\.12-slim", text, re.M)
    assert "pip install --no-cache-dir -r requirements" in text
    assert re.search(r"^WORKDIR /app$", text, re.M)
    assert "PYTHONUNBUFFERED=1" in text and "TZ=UTC" in text
    assert re.search(r"useradd .*-u 1000 .*shio", text)
    users = re.findall(r"^USER (\S+)", text, re.M)
    assert users and users[-1] == "shio"
    assert ("HEALTHCHECK --interval=60s --timeout=10s --start-period=180s "
            "CMD python -m engine health") in text
    assert re.search(r"^STOPSIGNAL SIGTERM$", text, re.M)
    assert 'CMD ["python", "-m", "engine", "run"]' in text.replace('","', '", "')


def test_env_example_names_without_values():
    lines = [l.strip() for l in _read(".env.example").splitlines()]
    assigns = [l for l in lines if l and not l.startswith("#")]
    assert sorted(assigns) == ["SHIO_BACKUP_KEY=", "SHIO_HEALTHCHECK_URL=", "SHIO_TELEGRAM_TOKEN="]
    comments = [l for l in lines if l.startswith("#")]
    assert comments and any(re.search("[а-яА-Я]", c) for c in comments)


def test_deploy_sh_syntax_and_commands():
    path = ROOT / "deploy.sh"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") or text.startswith("#!/bin/bash")
    assert "set -euo pipefail" in text
    for cmd in ("git pull --ff-only", "docker compose build", "docker compose up -d",
                "python -m engine status", "docker compose logs -f --tail 200",
                "docker compose run --rm engine python -m engine doctor",
                "docker compose stop engine", "docker compose start engine",
                "python -m engine restore", "--tail 50"):
        assert cmd in text, cmd
    assert path.stat().st_mode & 0o111, "deploy.sh должен быть исполняемым"
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_dockerignore():
    entries = {l.strip().rstrip("/") for l in _read(".dockerignore").splitlines()
               if l.strip() and not l.startswith("#")}
    for name in ("data", ".env", ".git", ".superpowers"):
        assert name in entries, name


def test_gitignore_backups():
    assert "data/backups/" in _read(".gitignore").splitlines()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker не установлен")
def test_compose_config_valid(tmp_path):
    # копия в tmp: не трогаем настоящий .env в корне проекта
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    r = subprocess.run(["docker", "compose", "config", "-q"], cwd=tmp_path,
                       capture_output=True, text=True)
    if r.returncode != 0 and ("not a docker command" in r.stderr
                              or "unknown command" in r.stderr):
        pytest.skip("нет плагина docker compose")
    assert r.returncode == 0, r.stderr
