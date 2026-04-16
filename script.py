#!/usr/bin/env python3
"""
script.py  –  Env-var injection script for Helm chart YAML files.

All inputs come from environment variables:

    export ENV=dev                    # single env, or "dev,qa", or "all"
    export KEY=REDIS_HOST
    export VALUE=redis.myapp.com
    export IS_SENSITIVE=false         # true → secret.yaml, false → configmap.yaml
                                      # omit to be prompted interactively
    export APP_NAME=notification-api  # exact container name in deployment.yaml
    export DRY_RUN=true               # true = skip git (default), false = branch+commit+push
    export OVERWRITE=true             # optional: skip the interactive overwrite prompt
    export BRANCH_NAME=my-branch      # optional: auto-generated if not set
    export GITHUB_TOKEN=ghp_xxx       # required when DRY_RUN=false
    export GITHUB_REPO=org/repo       # required when DRY_RUN=false
    export BASE_BRANCH=main           # optional: PR target branch (default: main)
    export REQUESTER=john@example.com # optional: shown in PR description
    export ADD_IF_CONDITION=true      # optional: wrap deployment block in {{- if .Values.<key> }}
                                      # auto-set to true when deploying to a subset of envs

    python3 script.py

Repo layout expected:
    templates/
        configmap.yaml
        secret.yaml
        deployment.yaml
    dev/
        dev-values.yaml
    qa/
        qa-values.yaml
    demo/
        demo-values.yaml

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
from pathlib import Path

from ruamel.yaml import YAML

# ──────────────────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────────────────
ALL_ENVS = ["dev", "qa", "demo"]

VALUES_FILENAME = {
    "dev":  "dev-values.yaml",
    "qa":   "qa-values.yaml",
    "demo": "demo-values.yaml",
}

TEMPLATES_DIR   = "templates"
CONFIGMAP_FILE  = os.path.join(TEMPLATES_DIR, "configmap.yaml")
SECRET_FILE     = os.path.join(TEMPLATES_DIR, "secret.yaml")
DEPLOYMENT_FILE = os.path.join(TEMPLATES_DIR, "deployment.yaml")

# ──────────────────────────────────────────────────────────────────────────────
#  YAML engine  (used for values files only — templates are raw-text patched)
# ──────────────────────────────────────────────────────────────────────────────
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)


# ──────────────────────────────────────────────────────────────────────────────
#  Utilities
# ──────────────────────────────────────────────────────────────────────────────
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


def read_text(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def write_text(path: str, content: str) -> None:
    with open(path, "w") as fh:
        fh.write(content)


def read_lines(path: str) -> list:
    with open(path) as fh:
        return fh.readlines()


def write_lines(path: str, lines: list) -> None:
    with open(path, "w") as fh:
        fh.writelines(lines)


def ensure_trailing_newline(lines: list) -> None:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"


def remove_key_block_from_lines(lines: list, key: str) -> list:
    """Remove an indented 'KEY: ...' entry and any immediately-following
    indented continuation lines (handles multi-line YAML values).
    Also removes any surrounding {{- if }} / {{- end }} wrapper for this key."""
    out = []
    skip = False
    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect a Helm if-wrapper that references this key
        if re.match(
            rf"^\s+{{{{-?\s*if\s+\.Values\.{re.escape(key.lower().replace('-', '_'))}\s*-?}}}}\s*$",
            line,
        ):
            # Skip the if-line, the key line(s), and the end-line
            i += 1
            while i < len(lines):
                inner = lines[i]
                i += 1
                if re.match(r"^\s+{{-?\s*end\s*-?}}\s*$", inner):
                    break
            continue

        if re.match(rf"^\s+{re.escape(key)}\s*:", line):
            skip = True
            i += 1
            continue

        if skip and re.match(r"^\s+\S", line):
            skip = False
        if not skip:
            out.append(line)
        i += 1
    return out


def get_existing_indented_value(lines: list, key: str) -> str | None:
    for line in lines:
        m = re.match(rf"^\s+{re.escape(key)}\s*:\s*(.+)", line)
        if m:
            return m.group(1).strip().strip('"')
    return None


def upsert_indented_line(lines: list, key: str, new_line: str) -> tuple[list, bool]:
    """Replace the first indented 'KEY: …' line, or append. Returns (lines, replaced)."""
    for idx, line in enumerate(lines):
        if re.match(rf"^\s+{re.escape(key)}\s*:", line):
            lines[idx] = new_line
            return lines, True
    ensure_trailing_newline(lines)
    lines.append(new_line)
    return lines, False


def upsert_indented_block(lines: list, key: str, new_block: str) -> tuple[list, bool]:
    """Replace an existing KEY entry (plain or if-wrapped) with new_block, or append.
    Returns (lines, replaced)."""
    helm_key = key.lower().replace("-", "_")

    # Check if an if-wrapped block exists for this key
    for idx, line in enumerate(lines):
        if re.match(
            rf"^\s+{{{{-?\s*if\s+\.Values\.{re.escape(helm_key)}\s*-?}}}}\s*$",
            line,
        ):
            # Find the matching {{- end }} and replace the whole block
            end_idx = idx + 1
            while end_idx < len(lines):
                if re.match(r"^\s+{{-?\s*end\s*-?}}\s*$", lines[end_idx]):
                    break
                end_idx += 1
            lines[idx : end_idx + 1] = [new_block]
            return lines, True

    # Check if a plain (non-wrapped) KEY line exists
    for idx, line in enumerate(lines):
        if re.match(rf"^\s+{re.escape(key)}\s*:", line):
            lines[idx] = new_block
            return lines, True

    # Not found – append
    ensure_trailing_newline(lines)
    lines.append(new_block)
    return lines, False


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 0  –  Read & validate inputs
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 0 – Read inputs")

KEY      = os.environ.get("KEY",   "").strip().upper()
VALUE    = os.environ.get("VALUE", "").strip()
APP_NAME = os.environ.get("APP_NAME", "").strip()
DRY_RUN  = os.environ.get("DRY_RUN", "true").strip().lower() in ("true", "yes", "1")

BRANCH_NAME   = os.environ.get("BRANCH_NAME",   "").strip()
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN",  "").strip()
GITHUB_REPO   = os.environ.get("GITHUB_REPO",   "").strip()
BASE_BRANCH   = os.environ.get("BASE_BRANCH",   "main").strip()
REQUESTER     = os.environ.get("REQUESTER",     "unknown").strip()

# ── ENV: "all", "dev,qa", or a single name ───────────────────────────────────
_env_raw = os.environ.get("ENV", "").strip().lower()
if _env_raw == "all":
    TARGET_ENVS = list(ALL_ENVS)
elif _env_raw:
    TARGET_ENVS = [e.strip() for e in _env_raw.split(",") if e.strip()]
else:
    TARGET_ENVS = []

# ── IS_SENSITIVE: prompt if not set ──────────────────────────────────────────
_is_sensitive_raw = os.environ.get("IS_SENSITIVE", "").strip().lower()
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

# ── OVERWRITE: honour env var; None means "decide per-env at values-file time" ─
_overwrite_raw = os.environ.get("OVERWRITE", "").strip().lower()
OVERWRITE: bool | None = (
    _overwrite_raw in ("true", "yes", "1") if _overwrite_raw else None
)

# ── ADD_IF_CONDITION ─────────────────────────────────────────────────────────
# Auto-true when only a subset of envs is targeted; can be forced via env var.
_add_if_raw = os.environ.get("ADD_IF_CONDITION", "").strip().lower()
if _add_if_raw in ("true", "yes", "1"):
    ADD_IF_CONDITION = True
elif _add_if_raw in ("false", "no", "0"):
    ADD_IF_CONDITION = False
else:
    # Auto-derive: if we're updating all envs → no if-condition needed
    ADD_IF_CONDITION = (sorted(TARGET_ENVS) != sorted(ALL_ENVS))

# ── Validate required fields ──────────────────────────────────────────────────
errors: list[str] = []
if not TARGET_ENVS:
    errors.append("ENV is required (e.g. dev, qa, demo, dev,qa, or all)")
else:
    invalid = [e for e in TARGET_ENVS if e not in ALL_ENVS]
    if invalid:
        errors.append(f"Unknown ENV value(s): {invalid}. Valid: {ALL_ENVS}")
if not KEY:
    errors.append("KEY is required")
if not VALUE:
    errors.append("VALUE is required")
if not APP_NAME:
    errors.append("APP_NAME is required")
if errors:
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)
    sys.exit(1)

# ── Pre-flight checks (only when DRY_RUN=false) ───────────────────────────────
if not DRY_RUN:
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        print("[ERROR] GITHUB_TOKEN is required when DRY_RUN=false.", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("GITHUB_REPO", "").strip():
        print("[ERROR] GITHUB_REPO is required when DRY_RUN=false (e.g. org/repo-name).", file=sys.stderr)
        sys.exit(1)

# ── Derived names ─────────────────────────────────────────────────────────────
TIMESTAMP = datetime.now().strftime("%Y%m%d%H%M%S")
if not BRANCH_NAME:
    BRANCH_NAME = f"env-update/{KEY.lower().replace('_', '-')}-{TIMESTAMP}"
    print(f"[INFO]  BRANCH_NAME not set – using: {BRANCH_NAME}")

# helm_key: lowercase + hyphens → underscores (safe for Go template .Values)
helm_key = KEY.lower().replace("-", "_")

print(f"  ENV(s)          : {', '.join(TARGET_ENVS)}")
print(f"  KEY             : {KEY}")
print(f"  VALUE           : {'*' * len(VALUE)}  (masked)")
print(f"  SENSITIVE       : {IS_SENSITIVE}")
print(f"  APP             : {APP_NAME}")
print(f"  DRY_RUN         : {DRY_RUN}")
print(f"  BRANCH          : {BRANCH_NAME}")
print(f"  ADD_IF_CONDITION: {ADD_IF_CONDITION}")
print(f"  REQUESTER       : {REQUESTER}")

# ──────────────────────────────────────────────────────────────────────────────
#  Validate template files exist
# ──────────────────────────────────────────────────────────────────────────────
missing_templates = [f for f in [CONFIGMAP_FILE, SECRET_FILE, DEPLOYMENT_FILE]
                     if not os.path.exists(f)]
if missing_templates:
    print(f"\n[ERROR] Missing template files: {missing_templates}", file=sys.stderr)
    print(f"        Run from repo root; '{TEMPLATES_DIR}/' must contain configmap.yaml, "
          f"secret.yaml, deployment.yaml.", file=sys.stderr)
    sys.exit(1)

# ── Extract metadata.name from configmap.yaml and secret.yaml ────────────────
def extract_metadata_name(path: str) -> str:
    """Read metadata.name from a Kubernetes YAML template file."""
    with open(path) as fh:
        for line in fh:
            m = re.match(r"^\s*name:\s*(.+)", line)
            if m:
                # Strip quotes and Helm template markers if any
                return m.group(1).strip().strip('"').strip("'")
    raise ValueError(f"Could not find 'name:' under metadata in {path}")

CONFIGMAP_REF_NAME = extract_metadata_name(CONFIGMAP_FILE)
SECRET_REF_NAME    = extract_metadata_name(SECRET_FILE)
print(f"[INFO]  ConfigMap ref name : {CONFIGMAP_REF_NAME}")
print(f"[INFO]  Secret ref name    : {SECRET_REF_NAME}")

# Validate per-env values files
missing_values: list[str] = []
for env in TARGET_ENVS:
    vf = os.path.join(env, VALUES_FILENAME[env])
    if not os.path.exists(vf):
        missing_values.append(vf)
if missing_values:
    print(f"\n[ERROR] Missing values files: {missing_values}", file=sys.stderr)
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 1  –  Existence check on shared templates ONLY
#             (no overwrite prompt here — that's handled per-env in STEP 3)
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 1 – Existence check (shared templates)")

cm_lines  = read_lines(CONFIGMAP_FILE)
sec_lines = read_lines(SECRET_FILE)

key_in_cm  = any(re.match(rf"^\s+{re.escape(KEY)}\s*:", l) for l in cm_lines)
key_in_sec = any(re.match(rf"^\s+{re.escape(KEY)}\s*:", l) for l in sec_lines)

if key_in_cm or key_in_sec:
    location     = "configmap.yaml" if key_in_cm else "secret.yaml"
    source_lines = cm_lines if key_in_cm else sec_lines
    old_value    = get_existing_indented_value(source_lines, KEY)

    print(f"\n[INFO]  Key '{KEY}' already exists in templates/{location}.")
    print(f"  Current template ref : {old_value}")
    print(f"  (Per-env values will be checked individually in STEP 3)")
    # Template will be updated unconditionally — the Helm ref doesn't change,
    # but we may need to move it between configmap ↔ secret if IS_SENSITIVE changed.
    TEMPLATE_KEY_EXISTS = True
else:
    print(f"[OK]    Key '{KEY}' is new in templates.")
    TEMPLATE_KEY_EXISTS = False


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 2  –  templates/configmap.yaml  or  templates/secret.yaml
#
#  FIX: ConfigMap entries are now wrapped in {{- if .Values.<helm_key> }}
#       just like deployment.yaml, so envs that don't set the value won't
#       get an empty key rendered in the ConfigMap.
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 2 – Update shared template (configmap.yaml / secret.yaml)")

# Re-read fresh copies
cm_lines  = read_lines(CONFIGMAP_FILE)
sec_lines = read_lines(SECRET_FILE)

if IS_SENSITIVE:
    # Secret template: {{ .Values.helm_key | b64enc }}
    helm_secret_ref = f"{{{{ .Values.{helm_key} | b64enc }}}}"

    if key_in_cm:
        write_lines(CONFIGMAP_FILE, remove_key_block_from_lines(cm_lines, KEY))
        print(f"[OK]    Removed '{KEY}' from configmap.yaml (moved to secret).")
        cm_lines = read_lines(CONFIGMAP_FILE)

    # Secrets don't typically use if-conditions (missing secret key = chart error),
    # so we keep a plain entry here.  If you want if-wrapping in secrets too,
    # mirror the configmap block below.
    new_secret_line = f"  {KEY}: {helm_secret_ref}\n"
    sec_lines, replaced = upsert_indented_line(sec_lines, KEY, new_secret_line)
    write_lines(SECRET_FILE, sec_lines)
    action = "Updated" if replaced else "Added"
    print(f"[OK]    {action} '{KEY}' in secret.yaml  (Helm ref: {helm_secret_ref})")

else:
    # ConfigMap: wrap in {{- if .Values.<helm_key> }} so envs without the value
    # don't get an empty key rendered.
    helm_ref = f"{{{{ .Values.{helm_key} }}}}"

    if key_in_sec:
        write_lines(SECRET_FILE, remove_key_block_from_lines(sec_lines, KEY))
        print(f"[OK]    Removed '{KEY}' from secret.yaml (moved to configmap).")
        sec_lines = read_lines(SECRET_FILE)

    if ADD_IF_CONDITION:
        new_cm_block = (
            f"  {{{{- if .Values.{helm_key} }}}}\n"
            f'  {KEY}: "{helm_ref}"\n'
            f"  {{{{- end }}}}\n"
        )
        cm_lines, replaced = upsert_indented_block(cm_lines, KEY, new_cm_block)
        write_lines(CONFIGMAP_FILE, cm_lines)
        action = "Updated" if replaced else "Added"
        print(
            f"[OK]    {action} '{KEY}' in configmap.yaml  "
            f"(if-wrapped Helm ref: {helm_ref})"
        )
    else:
        # All envs targeted → safe to add without an if-guard
        new_cm_line = f'  {KEY}: "{helm_ref}"\n'
        cm_lines, replaced = upsert_indented_line(cm_lines, KEY, new_cm_line)
        write_lines(CONFIGMAP_FILE, cm_lines)
        action = "Updated" if replaced else "Added"
        print(f"[OK]    {action} '{KEY}' in configmap.yaml  (Helm ref: {helm_ref})")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 3  –  Per-env values files
#
#  FIX: Overwrite check is now per-env.  If the key already exists in THIS
#       env's values file we prompt (or honour OVERWRITE env var).  Envs that
#       don't have the key yet are always written without any prompt.
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 3 – Update per-env values files")

for env in TARGET_ENVS:
    values_path = os.path.join(env, VALUES_FILENAME[env])

    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)

    with open(values_path) as fh:
        data = y.load(fh)

    if data is None:
        data = {}

    old = data.get(helm_key)

    if old is not None:
        # Key already exists in THIS env's values file → decide whether to overwrite
        masked_old = "*" * len(str(old)) if IS_SENSITIVE else old
        masked_new = "*" * len(VALUE)    if IS_SENSITIVE else VALUE

        print(f"\n[WARN]  [{env}] Key '{helm_key}' already exists in {values_path}.")
        print(f"  Current value : {masked_old}")
        print(f"  New value     : {masked_new}")

        env_overwrite: bool
        if OVERWRITE is not None:
            # Global OVERWRITE env var was set — honour it for every env
            env_overwrite = OVERWRITE
            print(f"  OVERWRITE={OVERWRITE} → {'overwriting' if OVERWRITE else 'skipping'}.")
        else:
            # Ask interactively for this specific env
            while True:
                ans = input(f"  Overwrite '{helm_key}' in [{env}]? (yes/no): ").strip().lower()
                if ans in ("yes", "y"):
                    env_overwrite = True
                    break
                elif ans in ("no", "n"):
                    env_overwrite = False
                    break
                else:
                    print("  Please enter yes or no.")

        if not env_overwrite:
            print(f"[INFO]  [{env}] Skipped — keeping existing value.")
            continue

    data[helm_key] = VALUE  # always plaintext; secret.yaml does b64enc via Helm

    with open(values_path, "w") as fh:
        y.dump(data, fh)

    action = f"Updated (was: {old!r})" if old is not None else "Added"
    masked = "*" * len(VALUE) if IS_SENSITIVE else VALUE
    print(f"[OK]    [{env}] {action} '{helm_key}: {masked}' in {values_path}")

envs_without_key = [e for e in ALL_ENVS if e not in TARGET_ENVS]
if envs_without_key:
    print(f"[INFO]  Key NOT added to: {', '.join(envs_without_key)}  "
          f"(ADD_IF_CONDITION={'yes' if ADD_IF_CONDITION else 'no — consider setting it'})")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 4  –  templates/deployment.yaml  (raw-text, indent-aware)
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 4 – Patch templates/deployment.yaml")

raw = read_text(DEPLOYMENT_FILE)

if f"name: {KEY}" in raw:
    print(f"[WARN]  '{KEY}' already referenced in deployment.yaml – skipping.")
else:
    ref_type = "secretKeyRef"    if IS_SENSITIVE else "configMapKeyRef"
    ref_name = SECRET_REF_NAME  if IS_SENSITIVE else CONFIGMAP_REF_NAME

    lines = raw.splitlines(keepends=True)

    found_container = False
    in_env_block    = False
    insert_at       = None
    env_indent_str  = ""

    for idx, line in enumerate(lines):
        if not found_container:
            if re.search(rf"^\s*-\s*name:\s*{re.escape(APP_NAME)}\s*$", line):
                found_container = True
            continue

        if not in_env_block:
            m = re.match(r"^(\s+)env:\s*$", line)
            if m:
                in_env_block    = True
                env_indent_str  = m.group(1)
            continue

        if line.strip() == "" or line.strip().startswith("#"):
            continue

        leading        = len(line) - len(line.lstrip())
        env_indent_len = len(env_indent_str)

        if leading <= env_indent_len:
            insert_at = idx
            break

    if insert_at is None and in_env_block:
        insert_at = len(lines)

    if not found_container:
        print(
            f"[ERROR] Container '{APP_NAME}' not found in deployment.yaml.\n"
            f"        Check APP_NAME matches exactly.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not in_env_block:
        print(
            f"[ERROR] No 'env:' block found under container '{APP_NAME}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Derive correct indent from the env block indentation (env + 2 extra spaces)
    item_indent = env_indent_str + "  "

    if ADD_IF_CONDITION:
        new_block = (
            f"{item_indent}{{{{- if .Values.{helm_key} }}}}\n"
            f"{item_indent}- name: {KEY}\n"
            f"{item_indent}  valueFrom:\n"
            f"{item_indent}    {ref_type}:\n"
            f"{item_indent}      name: {ref_name}\n"
            f"{item_indent}      key: {KEY}\n"
            f"{item_indent}{{{{- end }}}}\n"
        )
    else:
        new_block = (
            f"{item_indent}- name: {KEY}\n"
            f"{item_indent}  valueFrom:\n"
            f"{item_indent}    {ref_type}:\n"
            f"{item_indent}      name: {ref_name}\n"
            f"{item_indent}      key: {KEY}\n"
        )

    lines.insert(insert_at, new_block)
    write_text(DEPLOYMENT_FILE, "".join(lines))

    if_note = " (wrapped in {{- if .Values." + helm_key + " }})" if ADD_IF_CONDITION else ""
    print(f"[OK]    Injected '{KEY}' into container '{APP_NAME}'{if_note}")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 5  –  Git commit, push & raise PR
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 5 – Git commit, push & raise PR")

CHANGED_FILES = (
    [CONFIGMAP_FILE, SECRET_FILE, DEPLOYMENT_FILE]
    + [os.path.join(env, VALUES_FILENAME[env]) for env in TARGET_ENVS]
)

envs_label = ", ".join(TARGET_ENVS)

if DRY_RUN:
    print("[DRY_RUN] Skipping git operations.")
    print(f"[DRY_RUN] Would create branch : {BRANCH_NAME}")
    print(f"[DRY_RUN] Would stage         : {', '.join(CHANGED_FILES)}")
    print(f"[DRY_RUN] Would commit        : chore(env): add/update '{KEY}' in {APP_NAME} [{envs_label}]")
    print(f"[DRY_RUN] Would push to       : origin/{BRANCH_NAME}")
    print(f"[DRY_RUN] Would raise PR      : {BRANCH_NAME} → {BASE_BRANCH}")
else:
    commit_msg = (
        f"chore(env): add/update '{KEY}' in {APP_NAME} [{envs_label}]\n\n"
        f"Key            : {KEY}\n"
        f"Sensitive      : {IS_SENSITIVE}\n"
        f"Target app     : {APP_NAME}\n"
        f"Envs updated   : {envs_label}\n"
        f"If-condition   : {ADD_IF_CONDITION}\n"
        f"Branch         : {BRANCH_NAME}"
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
    pr_title = f"chore(env): add/update '{KEY}' in {APP_NAME} [{envs_label}]"
    pr_body = "\n".join([
        "## Env Variable Update",
        "",
        "| Field           | Value |",
        "|-----------------|-------|",
        f"| Key             | `{KEY}` |",
        f"| Sensitive       | `{IS_SENSITIVE}` |",
        f"| Target App      | `{APP_NAME}` |",
        f"| Envs Updated    | `{envs_label}` |",
        f"| If-Condition    | `{ADD_IF_CONDITION}` |",
        f"| Requester       | `{REQUESTER}` |",
        f"| Branch          | `{BRANCH_NAME}` |",
        "",
        "_Auto-generated by env-injection script._",
    ])

    payload = json.dumps({
        "title": pr_title,
        "head":  BRANCH_NAME,
        "base":  BASE_BRANCH,
        "body":  pr_body,
    }).encode()

    req = urllib.request.Request(
        url    = f"https://api.github.com/repos/{GITHUB_REPO}/pulls",
        data   = payload,
        method = "POST",
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
        },
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
print(f"  Key            : {KEY}")
print(f"  Sensitive      : {IS_SENSITIVE}")
print(f"  Target app     : {APP_NAME}")
print(f"  Envs updated   : {envs_label}")
print(f"  If-condition   : {ADD_IF_CONDITION}")
print(f"  Requester      : {REQUESTER}")
print(f"  Branch         : {BRANCH_NAME}  {'(not pushed – dry-run)' if DRY_RUN else '(pushed + PR raised)'}")
print()