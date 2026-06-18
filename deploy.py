#!/usr/bin/env python3
"""
deploy.py - Deploy the PID bot to Toolforge.

  Credentials come from your local machine (SFTP).
  Bot code comes from GitHub (toolforge build).

Usage:
  python deploy.py              # Upload creds + trigger build  (normal use)
  python deploy.py --creds-only # Only upload credential files
  python deploy.py --build-only # Only trigger toolforge build
  python deploy.py --run        # Deploy then run the bot immediately
  python deploy.py --status     # Show build / job status and exit
  python deploy.py --setup      # First-time: create remote dirs + load job
"""

import argparse
import getpass
import sys
import time
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS  (edit these once)
# ─────────────────────────────────────────────────────────────────────────────

WIKIMEDIA_USER = "r1f4t"
TOOL_NAME      = "pid-bangladesh"
GITHUB_URL     = "https://github.com/r1f4t/pid-bangladesh"  # your GitHub repo
BASTION        = "login.toolforge.org"
SSH_KEY        = Path.home() / ".ssh" / "id_ed25519"

# Where credentials are stored on Toolforge NFS (accessible at runtime via mount: all)
REMOTE_CREDS_DIR = "/data/project/pid-bangladesh"

# Credential files that live next to this script and must NOT go to GitHub
CREDENTIAL_FILES = [
    "gemini.key",
    "ia.key",
    "JSON.json",
    "drive_token.json",
    "drive_oauth_client.json",
    "user-config.py",
    "user-password.py",
]

SCRIPT_DIR = Path(__file__).parent.resolve()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
C = "\033[96m"; B = "\033[1m";  X = "\033[0m"

def ok(m):   print(f"  {G}✔{X}  {m}")
def warn(m): print(f"  {Y}!{X}  {m}")
def err(m):  print(f"  {R}✘{X}  {m}")
def log(m):  print(f"     {m}")
def hdr(m):
    print(f"\n{B}{C}{'─'*60}{X}")
    print(f"{B}  {m}{X}")
    print(f"{C}{'─'*60}{X}")


def connect(pin: str):
    """SSH connect to the Toolforge bastion as WIKIMEDIA_USER."""
    try:
        import paramiko
    except ImportError:
        err("paramiko is not installed. Run:  pip install paramiko")
        sys.exit(1)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=BASTION,
            username=WIKIMEDIA_USER,
            key_filename=str(SSH_KEY),
            passphrase=pin or None,
            timeout=30,
        )
    except paramiko.AuthenticationException:
        err("Authentication failed – wrong PIN / passphrase?")
        sys.exit(1)
    except Exception as e:
        err(f"Connection error: {e}")
        sys.exit(1)
    return client


def ssh(client, cmd: str, as_tool: bool = True):
    """Run a remote command, streaming output. Returns exit code."""
    full = f"become {TOOL_NAME} && {cmd}" if as_tool else cmd
    log(f"$ {full}")
    _, stdout, _ = client.exec_command(full, get_pty=True, timeout=300)
    for line in iter(stdout.readline, ""):
        line = line.rstrip()
        if line:
            log(line)
    return stdout.channel.recv_exit_status()


# ─────────────────────────────────────────────────────────────────────────────
# Deploy steps
# ─────────────────────────────────────────────────────────────────────────────

def step_upload_creds(client):
    hdr("STEP 1 of 2  –  Uploading credentials via SFTP")
    log(f"Destination: {REMOTE_CREDS_DIR}")

    sftp = client.open_sftp()
    uploaded = 0
    skipped  = 0
    try:
        for fname in CREDENTIAL_FILES:
            local = SCRIPT_DIR / fname
            if not local.exists():
                warn(f"{fname}  (not found locally – skipped)")
                skipped += 1
                continue
            remote = f"{REMOTE_CREDS_DIR}/{fname}"
            log(f"Uploading  {fname} ...")
            try:
                sftp.put(str(local), remote)
                ok(fname)
                uploaded += 1
            except Exception as e:
                err(f"{fname}: {e}")
    finally:
        sftp.close()

    print()
    ok(f"{uploaded} file(s) uploaded, {skipped} skipped")

    # Lock down permissions on all uploaded creds
    chmod = " && ".join(
        f"chmod 600 {REMOTE_CREDS_DIR}/{f}"
        for f in CREDENTIAL_FILES
        if (SCRIPT_DIR / f).exists()
    )
    if chmod:
        ssh(client, chmod, as_tool=False)   # run as personal user – dir is owned by tool
        ok("Permissions set to 600")


def step_build(client):
    hdr("STEP 2 of 2  –  Triggering Toolforge build from GitHub")
    log(f"Repo:  {GITHUB_URL}")
    log("Building container image (usually takes 2–5 minutes) ...")
    print()
    code = ssh(client, f"toolforge build start {GITHUB_URL}")
    if code == 0:
        ok("Build started!  Run  python deploy.py --status  to track progress.")
    else:
        warn(f"Build command returned exit code {code} – check output above.")


def step_run(client):
    hdr("Running the bot as a Toolforge job")
    code = ssh(client, "toolforge jobs run pid-bot")
    if code == 0:
        ok("Job submitted!")
        log("Tail logs with:")
        log(f'  ssh {WIKIMEDIA_USER}@{BASTION} "become {TOOL_NAME} && toolforge jobs logs pid-bot -f"')
    else:
        warn(f"Job submission returned exit code {code}")


def step_status(client):
    hdr("Build status")
    ssh(client, "toolforge build show")
    hdr("Job list")
    ssh(client, "toolforge jobs list")


def step_setup(client):
    """First-time: ensure the remote creds dir exists and register the job."""
    hdr("First-time setup")
    # Make sure the creds directory exists and has correct ownership
    ssh(client, f"mkdir -p {REMOTE_CREDS_DIR}", as_tool=False)
    # Register the Toolforge job definition (job.yaml must already exist in the repo on GitHub)
    ssh(client, "toolforge jobs load ~/www/python/src/toolforge/job.yaml 2>/dev/null || true")
    ok("Setup done – run  python deploy.py  to deploy.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Deploy PID bot to Toolforge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--creds-only", action="store_true", help="Only upload credential files")
    p.add_argument("--build-only", action="store_true", help="Only trigger toolforge build")
    p.add_argument("--run",        action="store_true", help="Run the bot after deploying")
    p.add_argument("--status",     action="store_true", help="Show build/job status")
    p.add_argument("--setup",      action="store_true", help="First-time remote setup")
    return p.parse_args()


def main():
    args = parse_args()

    print(f"\n{'='*60}")
    print("  PID Bot  ──  Toolforge Deploy")
    print(f"{'='*60}")
    print(f"  Wikimedia user : {WIKIMEDIA_USER}")
    print(f"  Tool account   : {TOOL_NAME}")
    print(f"  Bastion        : {BASTION}")
    print(f"  SSH key        : {SSH_KEY}")
    print(f"  Creds dir      : {REMOTE_CREDS_DIR}")
    print(f"  GitHub repo    : {GITHUB_URL}")
    print(f"{'='*60}\n")

    # Ask for key PIN once – never written to disk
    pin = getpass.getpass(f"  🔑  Enter PIN / passphrase for {SSH_KEY.name}: ")

    print(f"\n  Connecting to {BASTION} ...")
    client = connect(pin)
    ok(f"Connected as {WIKIMEDIA_USER}\n")

    try:
        if args.setup:
            step_setup(client)

        elif args.status:
            step_status(client)

        elif args.creds_only:
            step_upload_creds(client)

        elif args.build_only:
            step_build(client)

        else:
            # Default: upload creds THEN build
            step_upload_creds(client)
            step_build(client)

        if args.run and not args.status and not args.setup:
            print("\n  Waiting 5 s before submitting the job ...")
            time.sleep(5)
            step_run(client)

    finally:
        client.close()

    print(f"\n{'='*60}")
    print("  Done!")
    print(f"{'='*60}")
    print("  Handy commands:")
    print("    python deploy.py --status      # track build progress")
    print("    python deploy.py --creds-only  # re-upload creds after key rotation")
    print("    python deploy.py --run         # deploy + run immediately")
    print()


if __name__ == "__main__":
    main()
