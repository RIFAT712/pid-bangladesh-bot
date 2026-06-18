# Toolforge Deployment Guide

> **TL;DR — deploy from your local machine right now:**
> ```bash
> python deploy.py
> ```
> The script will ask for your Wikimedia username and tool name, then upload all credentials and trigger a container build in one step.

This project can be deployed to **Wikimedia Toolforge** in two ways:
| Method | When to use |
|--------|-------------|
| `python deploy.py` | Push code **and** credentials from your local machine |
| GitHub Actions (auto) | Push code changes — credentials stay on Toolforge |

---

## Method 1 — Local deploy with `deploy.py` ⬅️ *Start here*

Run this from your PC. It uploads credentials **and** triggers the Toolforge build in one command.

### Prerequisites
- Python 3 installed locally
- OpenSSH installed (comes with Windows 10/11 — check with `ssh -V` in PowerShell)
- A Toolforge tool account (create one at [toolsadmin.wikimedia.org](https://toolsadmin.wikimedia.org))
- Your SSH public key added to Toolforge (see Step 3 below)

### First run (interactive)

```powershell
python deploy.py
```

It will prompt you for:
- **Wikimedia username** — your personal wiki login (not the bot account)
- **Tool account name** — e.g. `pid-bangladesh`
- **GitHub repo URL** — e.g. `https://github.com/you/pid-bot`

### Save your settings (skip prompts next time)

Edit the `DEFAULTS` dict at the top of [deploy.py](deploy.py):

```python
DEFAULTS = {
    "wikimedia_user": "YourWikimediaUsername",
    "tool_name":      "pid-bangladesh",
    "remote_dir":     "",          # auto-computed if blank
    "github_url":     "https://github.com/you/pid-bot",
    "bastion":        "login.toolforge.org",
}
```

### Common commands

```powershell
# Full deploy: upload credentials + build image
python deploy.py

# Only re-upload credentials (after rotating a key)
python deploy.py --creds-only

# Only rebuild the image (credentials already on Toolforge)
python deploy.py --build-only

# Deploy and immediately run the bot
python deploy.py --run

# Check build status
python deploy.py --status
```

### What gets uploaded

| Local file | Destination on Toolforge |
|------------|--------------------------|
| `gemini.key` | `$TOOL_DATA_DIR/gemini.key` |
| `ia.key` | `$TOOL_DATA_DIR/ia.key` |
| `JSON.json` | `$TOOL_DATA_DIR/JSON.json` |
| `drive_token.json` | `$TOOL_DATA_DIR/drive_token.json` |
| `drive_oauth_client.json` | `$TOOL_DATA_DIR/drive_oauth_client.json` |
| `user-config.py` | `$TOOL_DATA_DIR/user-config.py` |
| `user-password.py` | `$TOOL_DATA_DIR/user-password.py` |

All files are `chmod 600` after upload. They are **never committed to git** (`.gitignore` blocks them).

---


```
git push → GitHub Actions → SSH → Toolforge bastion → git pull + pip install
```

Credentials **never leave Toolforge**. Only code changes flow through GitHub.

---

## One-time setup checklist

### Step 1 – Create a Toolforge tool account

1. Log in at [toolsadmin.wikimedia.org](https://toolsadmin.wikimedia.org/).
2. Create a tool (e.g. `pid-bangladesh`). This creates the UNIX account `tools.pid-bangladesh`.

---

### Step 2 – Generate a deploy SSH key pair (on your local machine)

```bash
ssh-keygen -t ed25519 -C "github-deploy-pid-bot" -f ~/.ssh/toolforge_deploy_key
# Creates:  ~/.ssh/toolforge_deploy_key      (private – goes to GitHub)
#           ~/.ssh/toolforge_deploy_key.pub   (public  – goes to Toolforge)
```

---

### Step 3 – Add the public key to Toolforge

```bash
# Log into the Toolforge bastion as yourself
ssh <YOUR_WIKIMEDIA_USERNAME>@login.toolforge.org

# Become the tool account
become pid-bangladesh          # replace with your tool name

# Append the public key
echo "ssh-ed25519 AAAA... github-deploy-pid-bot" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

---

### Step 4 – Clone the repo on Toolforge

```bash
# Still inside the tool account:
git clone https://github.com/<YOUR_GITHUB_USER>/<YOUR_REPO>.git ~/pid-bot
# Build the container image natively:
toolforge build start https://github.com/<YOUR_GITHUB_USER>/<YOUR_REPO>
```

---

### Step 5 – Copy credential files onto Toolforge

These files must be placed in `~/pid-bot/` on Toolforge **manually** (never commit them):

| File | What it is |
|------|-----------|
| `gemini.key` | Gemini AI Studio API key |
| `ia.key` | Internet Archive S3-like keys |
| `JSON.json` | Google Cloud service account key |
| `drive_token.json` | Google Drive OAuth2 token |
| `drive_oauth_client.json` | Google Drive OAuth2 client secrets |
| `user-config.py` | Pywikibot site config |
| `user-password.py` | Pywikibot bot password |

Copy with `scp` from your local machine:
```bash
scp gemini.key ia.key JSON.json drive_token.json drive_oauth_client.json \
    user-config.py user-password.py \
    <YOUR_WIKIMEDIA_USERNAME>@login.toolforge.org:/data/project/pid-bangladesh/pid-bot/
```

---

### Step 6 – Add secrets to GitHub

In your GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|-------------|-------|
| `TOOLFORGE_SSH_PRIVATE_KEY` | Contents of `~/.ssh/toolforge_deploy_key` (private key) |
| `TOOLFORGE_TOOL_NAME` | Your tool account name, e.g. `tools.pid-bangladesh` |
| `GITHUB_REPO_URL` | Full URL to your repository |

---

### Step 7 – Register the Toolforge job

```bash
# On Toolforge, inside the tool account:
toolforge jobs load ~/pid-bot/toolforge/job.yaml

# Run once to verify:
toolforge jobs run pid-bot

# Check logs:
cat ~/logs/pid-bot.log
```

To run on a schedule, uncomment the `schedule:` line in `toolforge/job.yaml`.

---

## Day-to-day workflow

```bash
# Edit code locally, then:
git add .
git commit -m "feat: improve separator detection"
git push origin main
# GitHub Actions fires and Toolforge pulls the update automatically
```

The **Actions** tab in GitHub shows the deployment status for every push.

---

## Running manually on Toolforge

```bash
ssh <YOUR_WIKIMEDIA_USERNAME>@login.toolforge.org
become pid-bangladesh
toolforge jobs run pid-bot          # one-off run
# or run the image manually interactively
toolforge jobs run pid-bot --command "run-bot"
```

---

## File structure

```
py codes/
├── .github/
│   └── workflows/
│       └── deploy.yml          ← GitHub Actions workflow
├── toolforge/
│   └── job.yaml                ← Toolforge job definition
├── .gitignore                  ← Keeps credentials out of git
├── requirements.txt            ← Python dependencies
├── Procfile                    ← Process definitions for the build service
├── main.py                     ← Bot entry point
└── ... (other modules)
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| SSH key refused | Check the key in `~/.ssh/authorized_keys` matches the GitHub secret |
| Build fails | Check logs with `toolforge build show` |
| Credentials not found | Check all 7 credential files are in `~/pid-bot/` on Toolforge |
| Job won't start | Run `toolforge jobs list` and check status |
