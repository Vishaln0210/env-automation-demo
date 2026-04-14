#!/usr/bin/env python3
"""
script.py  –  Env-var injection script for Helm chart YAML files.

All inputs come from environment variables:

    export ENV=dev
    export KEY=REDIS_HOST
    export VALUE=redis.myapp.com
    export IS_SENSITIVE=false        # true → secret.yaml, false → configmap.yaml
    export APP_NAME=notification-api # exact container name in deployment.yaml
    export DRY_RUN=true              # true = skip git (default), false = branch+commit+push
    export OVERWRITE=true            # optional: skip the interactive overwrite prompt
    export BRANCH_NAME=my-branch     # optional: auto-generated if not set
    export GITHUB_TOKEN=ghp_xxx      # required when DRY_RUN=false
    export GITHUB_REPO=org/repo      # required when DRY_RUN=false (e.g. "myorg/helm-charts")
    export BASE_BRANCH=main          # optional: PR target branch (default: main)
    export REQUESTER=john@example.com # optional: shown in PR description

    python3 script.py

Prerequisites:
    pip install ruamel.yaml
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

from ruamel.yaml import YAML

# ──────────────────────────────────────────────────────────────────────────────
#  YAML engine
# ──────────────────────────────────────────────────────────────────────────────
yaml = YAML()
yaml.preserve_quotes = True
yaml.default_flow_style = False
yaml.width = 4096
yaml.indent(mapping=2, sequence=4, offset=2)


def banner(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def run_git(cmd: list) -> None:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print(f"    {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"[ERROR] {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 0  –  Read & validate inputs
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 0 – Read inputs")

ENV_NAME    = os.environ.get("ENV",         "").strip()
KEY         = os.environ.get("KEY",         "").strip().upper()
VALUE       = os.environ.get("VALUE",       "").strip()
APP_NAME    = os.environ.get("APP_NAME",    "").strip()
DRY_RUN     = os.environ.get("DRY_RUN",    "true").strip().lower() in ("true", "yes", "1")
BRANCH_NAME = os.environ.get("BRANCH_NAME","").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "").strip()
BASE_BRANCH  = os.environ.get("BASE_BRANCH",  "main").strip()
REQUESTER    = os.environ.get("REQUESTER",    "unknown").strip()

# ── IS_SENSITIVE: prompt if not set ──────────────────────────────────────────
_is_sensitive_raw = os.environ.get("IS_SENSITIVE", "").strip().lower()
IS_SENSITIVE = None
if _is_sensitive_raw == "":
    print("\n[INPUT REQUIRED] IS_SENSITIVE was not set.")
    while True:
        ans = input("  Is this a sensitive value? (yes/no): ").strip().lower()
        if ans in ("yes", "y", "true", "1"):
            IS_SENSITIVE = True
            break
        elif ans in ("no", "n", "false", "0"):
            IS_SENSITIVE = False
            break
        else:
            print("  Please enter yes or no.")
else:
    IS_SENSITIVE = _is_sensitive_raw in ("true", "yes", "1")

# ── OVERWRITE: read from env but DO NOT prompt yet (done after existence check)
_overwrite_raw = os.environ.get("OVERWRITE", "").strip().lower()
OVERWRITE = _overwrite_raw in ("true", "yes", "1") if _overwrite_raw else None  # None = not decided yet

# ── Validate required fields ──────────────────────────────────────────────────
errors = []
if not ENV_NAME:  errors.append("ENV is required")
if not KEY:       errors.append("KEY is required")
if not VALUE:     errors.append("VALUE is required")
if not APP_NAME:  errors.append("APP_NAME is required")

if errors:
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)
    sys.exit(1)

# ── BRANCH_NAME: auto-generate if not provided ───────────────────────────────
TIMESTAMP = datetime.now().strftime("%Y%m%d%H%M%S")
if not BRANCH_NAME:
    BRANCH_NAME = f"env-update/{KEY.lower().replace('_', '-')}-{TIMESTAMP}"
    print(f"[INFO]  BRANCH_NAME not set – using: {BRANCH_NAME}")

helm_key = KEY.lower()   # REDIS_HOST → redis_host

print(f"  ENV        : {ENV_NAME}")
print(f"  KEY        : {KEY}")
print(f"  VALUE      : {'*' * len(VALUE)}  (masked)")
print(f"  SENSITIVE  : {IS_SENSITIVE}")
print(f"  APP        : {APP_NAME}")
print(f"  DRY_RUN    : {DRY_RUN}")
print(f"  BRANCH     : {BRANCH_NAME}")
print(f"  REQUESTER  : {REQUESTER}")

# ──────────────────────────────────────────────────────────────────────────────
#  File paths
# ──────────────────────────────────────────────────────────────────────────────
CONFIGMAP_FILE  = os.path.join(ENV_NAME, "configmap.yaml")
SECRET_FILE     = os.path.join(ENV_NAME, "secret.yaml")
VALUES_FILE     = os.path.join(ENV_NAME, "values.yaml")
DEPLOYMENT_FILE = os.path.join(ENV_NAME, "deployment.yaml")

missing = [f for f in [CONFIGMAP_FILE, SECRET_FILE, VALUES_FILE, DEPLOYMENT_FILE]
           if not os.path.exists(f)]
if missing:
    print(f"\n[ERROR] Missing files: {missing}", file=sys.stderr)
    print(f"        Run from repo root; folder '{ENV_NAME}/' must exist.", file=sys.stderr)
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────
def read_lines(path):
    with open(path) as fh:
        return fh.readlines()

def write_lines(path, lines):
    with open(path, "w") as fh:
        fh.writelines(lines)

def ensure_trailing_newline(lines):
    """Make sure the last line ends with \n so appends start on a fresh line."""
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

def remove_key_from_lines(lines, key):
    """Drop any line whose first non-space token is 'KEY:'."""
    return [l for l in lines if not re.match(rf"^\s+{re.escape(key)}\s*:", l)]

def upsert_indented_line(lines, key, new_line):
    """Replace indented 'KEY: ...' line, or append. Returns (lines, was_replaced)."""
    for idx, l in enumerate(lines):
        if re.match(rf"^\s+{re.escape(key)}\s*:", l):
            lines[idx] = new_line
            return lines, True
    ensure_trailing_newline(lines)
    lines.append(new_line)
    return lines, False

def upsert_toplevel_line(lines, key, new_line):
    """Replace top-level 'key: ...' line (no indent), or append. Returns (lines, was_replaced)."""
    for idx, l in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", l):
            lines[idx] = new_line
            return lines, True
    ensure_trailing_newline(lines)
    lines.append(new_line)
    return lines, False

def get_existing_value(lines, key):
    """Return the current value of a key from lines, or None."""
    for l in lines:
        m = re.match(rf"^\s+{re.escape(key)}\s*:\s*(.+)", l)
        if m:
            return m.group(1).strip().strip('"')
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 1  –  Existence check  +  interactive overwrite prompt
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 1 – Existence check")

cm_lines  = read_lines(CONFIGMAP_FILE)
sec_lines = read_lines(SECRET_FILE)

key_in_cm  = any(re.match(rf"^\s+{re.escape(KEY)}\s*:", l) for l in cm_lines)
key_in_sec = any(re.match(rf"^\s+{re.escape(KEY)}\s*:", l) for l in sec_lines)

if key_in_cm or key_in_sec:
    location     = "configmap.yaml" if key_in_cm else "secret.yaml"
    source_lines = cm_lines if key_in_cm else sec_lines
    old_value    = get_existing_value(source_lines, KEY)

    print(f"\n[WARN]  Key '{KEY}' already exists in {location}.")
    print(f"  Current value : {old_value}")
    print(f"  New value     : {'*' * len(VALUE) if IS_SENSITIVE else VALUE}")

    # If OVERWRITE was pre-set via env var, honour it; otherwise ask the user
    if OVERWRITE is None:
        while True:
            ans = input("\n  Overwrite existing value? (yes/no): ").strip().lower()
            if ans in ("yes", "y"):
                OVERWRITE = True
                break
            elif ans in ("no", "n"):
                OVERWRITE = False
                break
            else:
                print("  Please enter yes or no.")

    if not OVERWRITE:
        print("[INFO]  Aborted – no changes made.")
        sys.exit(0)

    print(f"[OK]    Overwriting '{KEY}'.")
else:
    print(f"[OK]    Key '{KEY}' is new.")
    OVERWRITE = False   # not relevant, but keep it defined


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 2  –  configmap.yaml  or  secret.yaml
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 2 – Update YAML source file")

if IS_SENSITIVE:
    b64_value = base64.b64encode(VALUE.encode()).decode()

    if key_in_cm:
        write_lines(CONFIGMAP_FILE, remove_key_from_lines(cm_lines, KEY))
        print(f"[OK]    Removed '{KEY}' from configmap.yaml.")

    sec_lines, replaced = upsert_indented_line(sec_lines, KEY, f"  {KEY}: {b64_value}\n")
    write_lines(SECRET_FILE, sec_lines)
    action = "Updated" if replaced else "Added"
    print(f"[OK]    {action} '{KEY}' in secret.yaml  (base64: {b64_value[:20]}…)")

else:
    helm_ref = f"{{{{ .Values.{helm_key} }}}}"

    if key_in_sec:
        write_lines(SECRET_FILE, remove_key_from_lines(sec_lines, KEY))
        print(f"[OK]    Removed '{KEY}' from secret.yaml.")

    cm_lines, replaced = upsert_indented_line(cm_lines, KEY, f'  {KEY}: "{helm_ref}"\n')
    write_lines(CONFIGMAP_FILE, cm_lines)
    action = "Updated" if replaced else "Added"
    print(f"[OK]    {action} '{KEY}' in configmap.yaml  (ref: {helm_ref})")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 3  –  values.yaml
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 3 – Update values.yaml")

val_lines = read_lines(VALUES_FILE)
val_lines, replaced = upsert_toplevel_line(val_lines, helm_key, f"{helm_key}: {VALUE}\n")
write_lines(VALUES_FILE, val_lines)
action = "Updated" if replaced else "Added"
print(f"[OK]    {action} '{helm_key}: {VALUE}' in values.yaml")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 4  –  deployment.yaml  (raw-text patch, Helm-safe)
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 4 – Patch deployment.yaml")

with open(DEPLOYMENT_FILE) as fh:
    raw = fh.read()

if f"name: {KEY}" in raw:
    print(f"[WARN]  '{KEY}' already referenced in deployment.yaml – skipping.")
else:
    indent   = "            "   # 12 spaces → inside containers[].env[]
    ref_type = "secretKeyRef"  if IS_SENSITIVE else "configMapKeyRef"
    ref_name = "cscs-secret"   if IS_SENSITIVE else "cscs-config"

    new_block = (
        f"{indent}- name: {KEY}\n"
        f"{indent}  valueFrom:\n"
        f"{indent}    {ref_type}:\n"
        f"{indent}      name: {ref_name}\n"
        f"{indent}      key: {KEY}\n"
    )

    lines = raw.splitlines(keepends=True)

    found_container = False
    in_env_block    = False
    insert_at       = None
    env_indent      = None

    for idx, line in enumerate(lines):
        if not found_container:
            if re.search(rf"^\s*-\s*name:\s*{re.escape(APP_NAME)}\b", line):
                found_container = True
            continue

        if not in_env_block:
            m = re.match(r"^(\s+)env:\s*$", line)
            if m:
                in_env_block = True
                env_indent   = m.group(1)
            continue

        if line.strip() == "":
            continue

        leading        = len(line) - len(line.lstrip())
        env_indent_len = len(env_indent)

        if leading <= env_indent_len and line.strip() and not line.strip().startswith("#"):
            insert_at = idx
            break

    if insert_at is None and in_env_block:
        insert_at = len(lines)

    if insert_at is None:
        print(
            f"[ERROR] Could not find container '{APP_NAME}' with an 'env:' block.\n"
            f"        Check APP_NAME matches the container name exactly.",
            file=sys.stderr,
        )
        sys.exit(1)

    lines.insert(insert_at, new_block)

    with open(DEPLOYMENT_FILE, "w") as fh:
        fh.write("".join(lines))

    print(f"[OK]    Injected '{KEY}' into container '{APP_NAME}' in deployment.yaml")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 5  –  Git commit, push & raise PR
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 5 – Git commit, push & raise PR")

CHANGED_FILES = [CONFIGMAP_FILE, SECRET_FILE, VALUES_FILE, DEPLOYMENT_FILE]

if DRY_RUN:
    print("[DRY_RUN] Skipping git operations.")
    print(f"[DRY_RUN] Would create branch : {BRANCH_NAME}")
    print(f"[DRY_RUN] Would stage         : {', '.join(CHANGED_FILES)}")
    print(f"[DRY_RUN] Would commit        : chore(env): add/update '{KEY}' in {APP_NAME}")
    print(f"[DRY_RUN] Would push to       : origin/{BRANCH_NAME}")
    print(f"[DRY_RUN] Would raise PR      : {BRANCH_NAME} → {BASE_BRANCH}")
else:
    if not GITHUB_TOKEN:
        print("[ERROR] GITHUB_TOKEN is required to raise a PR.", file=sys.stderr)
        sys.exit(1)
    if not GITHUB_REPO:
        print("[ERROR] GITHUB_REPO is required to raise a PR (e.g. org/repo-name).", file=sys.stderr)
        sys.exit(1)

    commit_msg = (
        f"chore(env): add/update '{KEY}' in {APP_NAME}\n\n"
        f"Key      : {KEY}\n"
        f"Sensitive: {IS_SENSITIVE}\n"
        f"Target   : {APP_NAME}\n"
        f"Branch   : {BRANCH_NAME}"
    )

    run_git(["git", "checkout", "-b", BRANCH_NAME])
    run_git(["git", "add"] + CHANGED_FILES)

    result = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True)
    if result.returncode != 0 and "nothing to commit" not in result.stdout.lower():
        print(f"[ERROR] git commit failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    run_git(["git", "push", "-u", "origin", BRANCH_NAME])
    print(f"[OK]    Branch '{BRANCH_NAME}' pushed to origin.")

    # ── Raise PR via GitHub API ───────────────────────────────────────────────
    pr_title = f"chore(env): add/update '{KEY}' in {APP_NAME}"
    pr_body  = (
        f"## Env Variable Update\n\n"
        f"| Field      | Value             |\n"
        f"|------------|-------------------|\n"
        f"| Key        | `{KEY}`           |\n"
        f"| Sensitive  | `{IS_SENSITIVE}`  |\n"
        f"| Target App | `{APP_NAME}`      |\n"
        f"| Requester  | `{REQUESTER}`     |\n"
        f"| Branch     | `{BRANCH_NAME}`   |\n\n"
        f"_Auto-generated by env-injection script._"
    )

    payload = json.dumps({
        "title": pr_title,
        "head":  BRANCH_NAME,
        "base":  BASE_BRANCH,
        "body":  pr_body,
    }).encode()

    req = urllib.request.Request(
        url     = f"https://api.github.com/repos/{GITHUB_REPO}/pulls",
        data    = payload,
        method  = "POST",
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req) as resp:
            pr = json.loads(resp.read().decode())
            print(f"[OK]    PR created : {pr['html_url']}")
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"[ERROR] GitHub PR creation failed ({e.code}): {err}", file=sys.stderr)
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
banner("Done ✓")
print(f"  Key      : {KEY}")
print(f"  Sensitive: {IS_SENSITIVE}")
print(f"  Target   : {APP_NAME}")
print(f"  Requester: {REQUESTER}")
print(f"  Branch   : {BRANCH_NAME}  {'(not pushed – dry-run)' if DRY_RUN else '(pushed + PR raised)'}")
print()