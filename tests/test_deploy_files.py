"""Файлы развёртывания: Dockerfile, docker-compose.yml, .env.example, deploy.sh, .dockerignore."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"


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
    assert "./config.local.yaml:/app/config.local.yaml:ro" in svc["volumes"]
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


def test_gitignore_backups_and_local_config():
    lines = _read(".gitignore").splitlines()
    assert "data/backups/" in lines and "config.local.yaml" in lines


def test_dockerfile_notes_mounted_config():
    assert "config.local.yaml" in _read("Dockerfile")


def test_readme_telegram_uses_local_config():
    readme = _read("README.md")
    section = readme[readme.index("## Telegram: подключение"):]
    section = section[:section.index("\n## ", 5)]
    assert "config.local.yaml" in section


def test_deploy_sh_structure():
    text = _read("deploy.sh")
    assert re.search(r"^main\(\) \{", text, re.M)
    assert text.rstrip().endswith('main "$@"\nexit $?')
    assert "HEALTH_WAIT_S=150" in text
    assert 'cid="$(container_id 2>/dev/null || true)"' in text
    assert "command -v docker" in text and "docker compose version" in text


# --- поведение deploy.sh с подменёнными docker и git ---
STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$CALLS"
if [[ "$(basename "$0")" == git ]]; then
    [[ -n "${GIT_FAIL:-}" ]] && { echo "fatal: Not possible to fast-forward" >&2; exit 1; }
    exit 0
fi
case "$*" in
    "compose ps -q engine") echo abc ;;
    inspect*Health*) echo healthy ;;
    inspect*) echo running ;;
esac
exit 0
"""


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / "data" / "backups").mkdir(parents=True)
    for d in (p / "data", p / "data" / "backups"):
        d.chmod(0o777)  # запись для uid 1000 при любом владельце
    shutil.copy(ROOT / "deploy.sh", p / "deploy.sh")
    (p / "config.yaml").write_text("capital_usd: 1\n", encoding="utf-8")
    (p / ".env").write_text("", encoding="utf-8")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("docker", "git"):
        (bin_ / name).write_text(STUB, encoding="utf-8")
        (bin_ / name).chmod(0o755)
    return p


def run_deploy(proj, *args, **env):
    import os
    calls = proj.parent / "calls.log"
    calls.write_text("", encoding="utf-8")
    full_env = {**os.environ, "PATH": f"{proj.parent / 'bin'}:{os.environ['PATH']}",
                "CALLS": str(calls), **env}
    r = subprocess.run([BASH, str(proj / "deploy.sh"), *args], cwd=proj, env=full_env,
                       capture_output=True, text=True, timeout=30)
    return r, calls.read_text(encoding="utf-8").splitlines()


def compose_calls(calls):
    return [c for c in calls if c.startswith("docker compose") and c != "docker compose version"]


def test_deploy_ok_creates_empty_local_config(proj):
    r, calls = run_deploy(proj)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (proj / "config.local.yaml").is_file()
    assert (proj / "config.local.yaml").read_text() == ""
    assert "git pull --ff-only" in calls
    assert "docker compose exec engine python -m engine status" in calls


@pytest.mark.parametrize("cmd", [[], ["logs"], ["doctor"]])
def test_missing_config_yaml_stops_before_compose(proj, cmd):
    (proj / "config.yaml").unlink()
    r, calls = run_deploy(proj, *cmd)
    assert r.returncode != 0 and "config.yaml" in r.stderr
    assert compose_calls(calls) == [] and "git pull --ff-only" not in calls


def test_config_yaml_directory_stops(proj):
    (proj / "config.yaml").unlink()
    (proj / "config.yaml").mkdir()
    r, calls = run_deploy(proj, "doctor")
    assert r.returncode != 0 and "config.yaml" in r.stderr and compose_calls(calls) == []


def test_local_config_directory_stops(proj):
    (proj / "config.local.yaml").mkdir()
    r, calls = run_deploy(proj, "logs")
    assert r.returncode != 0 and "config.local.yaml" in r.stderr and compose_calls(calls) == []


def test_missing_env_stops(proj):
    (proj / ".env").unlink()
    r, calls = run_deploy(proj, "logs")
    assert r.returncode != 0 and ".env.example" in r.stderr and compose_calls(calls) == []


def test_git_pull_failure_clear_message(proj):
    r, calls = run_deploy(proj, GIT_FAIL="1")
    assert r.returncode != 0
    assert "git status" in r.stderr and "config.local.yaml" in r.stderr
    assert compose_calls(calls) == []


def test_no_docker_message(proj, tmp_path):
    import os
    farm = tmp_path / "nodocker"  # все программы из PATH, кроме docker
    farm.mkdir()
    for d in os.environ["PATH"].split(":"):
        if not Path(d).is_dir():
            continue
        for exe in Path(d).iterdir():
            target = farm / exe.name
            if exe.name != "docker" and not target.exists() and not target.is_symlink():
                target.symlink_to(exe)
    r, _ = run_deploy(proj, "logs", PATH=str(farm))
    assert r.returncode != 0 and "Docker" in r.stderr


def test_restore_mounts_file_read_only_and_restarts(proj, tmp_path):
    src = tmp_path / "shio-2026-11-01.db.enc"
    src.write_bytes(b"x")
    r, calls = run_deploy(proj, "restore", str(src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert list((proj / "data" / "backups").iterdir()) == []  # ничего не копируется
    seq = compose_calls(calls)
    i = seq.index("docker compose stop engine")
    j = next(k for k, c in enumerate(seq) if "python -m engine restore" in c)
    k = seq.index("docker compose start engine")
    assert i < j < k
    assert seq[j] == (f"docker compose run --rm -v {src.resolve()}:/tmp/restore.db.enc:ro engine "
                      "python -m engine restore --force /tmp/restore.db.enc")


def test_restore_file_inside_data_also_mounted(proj):
    src = proj / "data" / "backups" / "shio-2026-11-02.db.enc"
    src.write_bytes(b"x")
    r, calls = run_deploy(proj, "restore", "data/backups/shio-2026-11-02.db.enc")
    assert r.returncode == 0, r.stdout + r.stderr
    j = next(c for c in compose_calls(calls) if "python -m engine restore" in c)
    assert f"-v {src.resolve()}:/tmp/restore.db.enc:ro" in j


def test_restore_missing_file(proj):
    r, calls = run_deploy(proj, "restore", "nope.db.enc")
    assert r.returncode != 0 and "не найден" in r.stderr and compose_calls(calls) == []


def test_restart_recreates_without_pull_or_build(proj):
    r, calls = run_deploy(proj, "restart")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(c.startswith("git") for c in calls)
    seq = compose_calls(calls)
    assert "docker compose build" not in seq
    i = seq.index("docker compose up -d --force-recreate engine")
    j = seq.index("docker compose exec engine python -m engine status")
    assert i < j
    assert (proj / "config.local.yaml").is_file()  # проверки prepare() выполнены


def test_restart_missing_env_stops(proj):
    (proj / ".env").unlink()
    r, calls = run_deploy(proj, "restart")
    assert r.returncode != 0 and ".env" in r.stderr
    assert compose_calls(calls) == []


def test_restart_unhealthy_shows_logs(proj):
    stub = proj.parent / "bin" / "docker"
    stub.write_text(STUB.replace("echo healthy", "echo unhealthy").replace(
        "inspect*) echo running", "inspect*) echo exited"), encoding="utf-8")
    r, calls = run_deploy(proj, "restart")
    assert r.returncode != 0
    assert "docker compose logs --no-color --tail 50 engine" in calls


def test_help_lists_restart(proj):
    r, _ = run_deploy(proj, "help")
    assert r.returncode == 0 and "./deploy.sh restart" in r.stdout
    r, _ = run_deploy(proj, "bogus")
    assert "restart" in r.stderr


def test_install_guide_uses_restart():
    guide = _read("docs/INSTALL.md")
    assert "./deploy.sh restart" in guide
    assert "--force-recreate" not in guide


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


def test_install_guide_final_review_points():
    guide = _read("docs/INSTALL.md")
    for text in ("## 0. Перед началом", "sudo rm /etc/ssh/sshd_config.d/00-shio.conf",
                 "НЕ делайте шаг 2.5", "Enter passphrase for key", "id -u shio",
                 "docker ps", "sudo apt install -y git", "ls /var/run/reboot-required",
                 "Telegram Desktop", "(unhealthy)", "Движок завис N с — перезапуск",
                 "DigitalOcean, Vultr или Netcup"):
        assert text in guide, text
    assert guide.index("sudo apt install -y git") < guide.index("git clone")
