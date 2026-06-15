"""
vps_deploy.py - Deployer une mise a jour Bonaza sur le VPS (consolidateur).

Remplace les nombreux scripts vps_phase*.py / vps_push_*.py / vps_recreate.py.

Usage :
  python src/vps_deploy.py                 # full: push src + rebuild + recreate
  python src/vps_deploy.py --no-build      # push sans rebuild (changement code only)
  python src/vps_deploy.py --no-restart    # push + build sans restart containers
  python src/vps_deploy.py --bot-only      # restart juste bonaza_bot + dashboard
  python src/vps_deploy.py --main-only     # restart juste bonaza_main
  python src/vps_deploy.py --stop-main     # stop bonaza_main (avant deblocage IG, etc.)
  python src/vps_deploy.py --start-main    # start bonaza_main (apres deblocage IG)
  python src/vps_deploy.py --status        # juste docker ps + dernieres lignes log

Pre-requis : .diddess.local (credentials SSH DIDDESS) + cle privee bonaza_diddess_id

Fichiers transferes par defaut :
  src/*.py             -> /opt/bonaza/src/
  requirements.txt     -> /opt/bonaza/requirements.txt
  Dockerfile           -> /opt/bonaza/Dockerfile
  docker-compose.yml   -> /opt/bonaza/docker-compose.yml
  (PAS le .env : voir vps_deploy.py --env si besoin)

Le .env sur le VPS n'est PAS touche par defaut (sensible). Pour le modifier,
utiliser SSH directement ou ajouter --env.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import paramiko

# Charger credentials DIDDESS
ROOT = Path(__file__).parent.parent
DIDDESS_FILE = ROOT / ".diddess.local"
if not DIDDESS_FILE.exists():
    print("ERREUR : .diddess.local introuvable")
    sys.exit(1)

cfg = {}
for line in DIDDESS_FILE.read_text(encoding="utf-8").splitlines():
    if ":" in line and not line.strip().startswith("#"):
        k, v = line.split(":", 1)
        cfg[k.strip().lower()] = v.strip()

HOST = cfg["host"]
USER = cfg["user"]
KEY  = cfg["key_file"]
REMOTE_ROOT = "/opt/bonaza"


def connect() -> paramiko.SSHClient:
    pkey = paramiko.Ed25519Key.from_private_key_file(KEY)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=HOST, username=USER, pkey=pkey, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c


def run(client, cmd: str, label: str = "", timeout: int = 60) -> tuple[int, str, str]:
    if label:
        print(f"# {label}")
    print(f"$ {cmd[:200]}")
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace").rstrip()
    err = stderr.read().decode(errors="replace").rstrip()
    rc = stdout.channel.recv_exit_status()
    if out: print(out[:2000])
    if err: print(f"[stderr] {err[:1000]}")
    return rc, out, err


def transfer(client) -> int:
    """Push src/*.py + fichiers racine vers /opt/bonaza."""
    print("\n=== TRANSFERT ===")
    sftp = client.open_sftp()
    n = 0
    for f in ["Dockerfile", "docker-compose.yml", ".dockerignore", "requirements.txt"]:
        p = ROOT / f
        if p.exists():
            sftp.put(str(p), f"{REMOTE_ROOT}/{f}")
            print(f"  -> {f}")
            n += 1
    # src/ : tout sauf vps_*, _*, test_*
    excludes = {"__pycache__"}
    skip_prefix = ("vps_", "_", "test_")
    for item in (ROOT / "src").rglob("*.py"):
        if any(p in excludes for p in item.parts):
            continue
        name = item.name
        if any(name.startswith(p) for p in skip_prefix):
            # Sauf vps_deploy.py et telegram_setup_helper.py + telegram_bot.py
            if name not in ("vps_deploy.py", "telegram_setup_helper.py", "telegram_bot.py"):
                continue
        rel = "/".join(item.parts[len(ROOT.parts):])
        sftp.put(str(item), f"{REMOTE_ROOT}/{rel}")
        print(f"  -> {rel}")
        n += 1
    sftp.close()
    print(f"({n} fichiers transferes)")
    return n


def build(client) -> int:
    """docker-compose build en arriere-plan + polling."""
    print("\n=== BUILD ===")
    run(client, (
        f"cd {REMOTE_ROOT} && rm -f build.log build.done && "
        "(nohup bash -c 'docker-compose build 2>&1 ; "
        f"echo EXIT_$? > {REMOTE_ROOT}/build.done' "
        f"> {REMOTE_ROOT}/build.log 2>&1 < /dev/null &) && sleep 1"
    ), label="lancement build async")

    start = time.time()
    for _ in range(60):  # max 20 min
        time.sleep(20)
        elapsed = int(time.time() - start)
        _, st, _ = client.exec_command(f"cat {REMOTE_ROOT}/build.done 2>/dev/null || echo NOT")
        s = st.read().decode().strip()
        _, ll, _ = client.exec_command(f"tail -n 1 {REMOTE_ROOT}/build.log 2>/dev/null")
        last = ll.read().decode().strip()[:120]
        print(f"  [{elapsed:4d}s] {last}")
        if s.startswith("EXIT_"):
            rc = int(s.replace("EXIT_", ""))
            print(f"  -> exit {rc}")
            return rc
    print("  TIMEOUT 20 min")
    return -1


def status(client) -> None:
    print("\n=== STATUS ===")
    run(client, "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'", label="docker ps")
    run(client, "docker logs --tail=15 bonaza_main 2>&1 | tail -15", label="bonaza_main last 15")
    run(client, "docker logs --tail=10 bonaza_dashboard 2>&1 | tail -10", label="bonaza_dashboard last 10")
    run(client, "docker logs --tail=10 bonaza_bot 2>&1 | tail -10", label="bonaza_bot last 10")


def main():
    p = argparse.ArgumentParser(description="Deploy Bonaza vers VPS")
    p.add_argument("--no-build",    action="store_true")
    p.add_argument("--no-restart",  action="store_true")
    p.add_argument("--bot-only",    action="store_true",
                   help="Recree bot + dashboard (pas bonaza_main)")
    p.add_argument("--main-only",   action="store_true",
                   help="Recree juste bonaza_main")
    p.add_argument("--stop-main",   action="store_true",
                   help="Stop bonaza_main et exit")
    p.add_argument("--start-main",  action="store_true",
                   help="Start bonaza_main et exit")
    p.add_argument("--status",      action="store_true",
                   help="Affiche docker ps + logs et exit")
    args = p.parse_args()

    client = connect()
    print(f"Connecte {USER}@{HOST}\n")

    try:
        if args.status:
            status(client)
            return

        if args.stop_main:
            run(client, f"cd {REMOTE_ROOT} && docker-compose stop bonaza_main 2>&1",
                label="STOP bonaza_main")
            run(client, "docker ps --format '{{.Names}} {{.Status}}'")
            return

        if args.start_main:
            run(client, f"cd {REMOTE_ROOT} && docker-compose start bonaza_main 2>&1",
                label="START bonaza_main", timeout=60)
            time.sleep(5)
            status(client)
            return

        transfer(client)

        if not args.no_build:
            rc = build(client)
            if rc != 0:
                print(f"\n[!!] build a echoue (exit {rc}). Voir build.log :")
                run(client, f"tail -20 {REMOTE_ROOT}/build.log")
                sys.exit(rc)

        if not args.no_restart:
            print("\n=== RESTART CONTAINERS ===")
            if args.bot_only:
                cmd = (f"cd {REMOTE_ROOT} && "
                       "docker-compose up -d --no-deps bonaza_bot bonaza_dashboard 2>&1")
            elif args.main_only:
                cmd = (f"cd {REMOTE_ROOT} && "
                       "docker-compose up -d --no-deps --force-recreate bonaza_main 2>&1")
            else:
                cmd = (f"cd {REMOTE_ROOT} && "
                       "docker-compose up -d 2>&1")
            run(client, cmd, timeout=120)
            time.sleep(5)

        status(client)

    finally:
        client.close()


if __name__ == "__main__":
    main()
