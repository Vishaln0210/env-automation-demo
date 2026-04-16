#!/usr/bin/env python3
"""
script.py  –  Env-var injection for Helm and non-Helm (raw K8s manifest) projects.

Environment variables:

    export PROJECT=cscs               # project name — must match projects.csv
    export ENV=dev                    # single env, comma-list, or "all"
    export KEY=REDIS_HOST
    export VALUE=redis.myapp.com
    export IS_SENSITIVE=false         # true → secret file, false → configmap
                                      # omit to be prompted interactively
    export APP_NAME=notification-api  # service name
                                      #   Helm     → container name in deployment.yaml
                                      #   non-Helm → maps to <project>-<APP_NAME>-backendconfig.yaml
                                      #              container name inside that file must equal APP_NAME
    export DRY_RUN=true               # true = skip git (default)
    export OVERWRITE=true             # optional: skip interactive overwrite prompt
    export BRANCH_NAME=my-branch      # optional: auto-generated if not set
    export GITHUB_TOKEN=ghp_xxx       # required when DRY_RUN=false
    export GITHUB_REPO=org/repo       # required when DRY_RUN=false
    export BASE_BRANCH=main           # optional (default: main)
    export REQUESTER=john@example.com # optional: shown in PR description
    export ADD_IF_CONDITION=true      # Helm only: wrap in {{- if .Values.<key> }}
                                      # auto-true when targeting subset of envs

    python3 script.py

── Repo layout ──────────────────────────────────────────────────────────────

projects.csv                          ← project registry

cscs/                                 ← Helm project
    templates/
        configmap.yaml
        secret.yaml
        deployment.yaml
    dev/dev-values.yaml
    qa/qa-values.yaml
    demo/demo-values.yaml

flat5-dev/                            ← non-Helm env folder (one per env)
    configmap.yaml                    ← plain  KEY: "value"
    flat5-secret.yaml                 ← plain  KEY: b64value
    flat5-auth-server-backendconfig.yaml
    flat5-notification-api-backendconfig.yaml
    flat5-asset-management-api-backendconfig.yaml

flat5-stage/
    configmap.yaml
    flat5-secret.yaml
    flat5-auth-server-backendconfig.yaml
    ...

── projects.csv columns ─────────────────────────────────────────────────────

    project     – project name
    server_name – environment  (dev / qa / stage / demo …)
    directory   – path to env folder relative to repo root
    helm        – true / false
    secret_file – (non-Helm only) secret filename inside the env folder
                  e.g. flat5-secret.yaml   (leave blank for Helm rows)

── Non-Helm convention ──────────────────────────────────────────────────────

    The APP_NAME value must equal the container name field inside the
    corresponding backendconfig YAML file.  The script validates this at
    startup and exits early if no matching container is found.

Prerequisites:
    pip install ruamel.yaml
"""

import base64
import csv
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
#  Constants
# ──────────────────────────────────────────────────────────────────────────────
PROJECTS_CSV = "projects.csv"

# Helm template paths (relative to <project>/)
HELM_TEMPLATES_DIR   = "templates"
HELM_CONFIGMAP_FILE  = os.path.join(HELM_TEMPLATES_DIR, "configmap.yaml")
HELM_SECRET_FILE     = os.path.join(HELM_TEMPLATES_DIR, "secret.yaml")
HELM_DEPLOYMENT_FILE = os.path.join(HELM_TEMPLATES_DIR, "deployment.yaml")

# non-Helm: configmap is always named configmap.yaml inside the env folder
NON_HELM_CONFIGMAP_FILENAME = "configmap.yaml"


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


def extract_metadata_name(path: str) -> str:
    """Return the value of the first 'name:' line in a K8s YAML file."""
    with open(path) as fh:
        for line in fh:
            m = re.match(r"^\s*name:\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise ValueError(f"Could not find 'name:' in {path}")


# ── YAML helpers ──────────────────────────────────────────────────────────────
def make_yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_yaml_file(path: str):
    y = make_yaml()
    with open(path) as fh:
        data = y.load(fh)
    return y, data or {}


def dump_yaml_file(y, data, path: str) -> None:
    with open(path, "w") as fh:
        y.dump(data, fh)


# ── Line-level helpers for template patching ─────────────────────────────────
def get_existing_indented_value(lines: list, key: str) -> str | None:
    for line in lines:
        m = re.match(rf"^\s+{re.escape(key)}\s*:\s*(.+)", line)
        if m:
            return m.group(1).strip().strip('"')
    return None


def remove_key_block_from_lines(lines: list, key: str) -> list:
    """Remove a KEY entry plus any surrounding {{- if }} / {{- end }} wrapper."""
    helm_key_pat = re.escape(key.lower().replace("-", "_"))
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        # Helm if-wrapper for this key
        if re.match(rf"^\s+{{{{-?\s*if\s+\.Values\.{helm_key_pat}\s*-?}}}}\s*$", line):
            i += 1
            while i < len(lines):
                if re.match(r"^\s+{{-?\s*end\s*-?}}\s*$", lines[i]):
                    i += 1
                    break
                i += 1
            continue
        # Plain key line
        if re.match(rf"^\s+{re.escape(key)}\s*:", line):
            i += 1
            while i < len(lines) and re.match(r"^\s{4,}", lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    return out


def upsert_indented_line(lines: list, key: str, new_line: str) -> tuple[list, bool]:
    for idx, line in enumerate(lines):
        if re.match(rf"^\s+{re.escape(key)}\s*:", line):
            lines[idx] = new_line
            return lines, True
    ensure_trailing_newline(lines)
    lines.append(new_line)
    return lines, False


def upsert_indented_block(lines: list, key: str, new_block: str) -> tuple[list, bool]:
    helm_key_pat = re.escape(key.lower().replace("-", "_"))
    # Replace if-wrapped block
    for idx, line in enumerate(lines):
        if re.match(rf"^\s+{{{{-?\s*if\s+\.Values\.{helm_key_pat}\s*-?}}}}\s*$", line):
            end_idx = idx + 1
            while end_idx < len(lines):
                if re.match(r"^\s+{{-?\s*end\s*-?}}\s*$", lines[end_idx]):
                    break
                end_idx += 1
            lines[idx: end_idx + 1] = [new_block]
            return lines, True
    # Replace plain key line
    for idx, line in enumerate(lines):
        if re.match(rf"^\s+{re.escape(key)}\s*:", line):
            lines[idx] = new_block
            return lines, True
    ensure_trailing_newline(lines)
    lines.append(new_block)
    return lines, False


# ── Non-Helm: raw K8s manifest helpers ───────────────────────────────────────
def _nonhelm_key_in_data_section(lines: list, key: str) -> bool:
    """True if KEY already exists in the data: section of a raw K8s manifest."""
    in_data = False
    for line in lines:
        if re.match(r"^data:\s*$", line):
            in_data = True
            continue
        if in_data:
            if re.match(r"^\S", line) and not line.strip().startswith("#"):
                in_data = False
            elif re.match(rf"^\s+{re.escape(key)}\s*:", line):
                return True
    return False


def nonhelm_key_in_configmap(lines: list, key: str) -> bool:
    """True if KEY already exists in the data: section of a raw ConfigMap."""
    return _nonhelm_key_in_data_section(lines, key)


def nonhelm_key_in_secret(lines: list, key: str) -> bool:
    """True if KEY already exists in the data: section of a raw Secret."""
    return _nonhelm_key_in_data_section(lines, key)


def nonhelm_upsert_configmap(lines: list, key: str, value: str) -> tuple[list, bool]:
    """Add or update KEY: "value" in the data: section of a raw ConfigMap."""
    new_line = f'  {key}: "{value}"\n'
    in_data = False
    for idx, line in enumerate(lines):
        if re.match(r"^data:\s*$", line):
            in_data = True
            continue
        if in_data:
            # Hit a top-level key → insert before it
            if re.match(r"^\S", line) and not line.strip().startswith("#"):
                lines.insert(idx, new_line)
                return lines, False
            if re.match(rf"^\s+{re.escape(key)}\s*:", line):
                lines[idx] = new_line
                return lines, True
    # data: was last section — append
    ensure_trailing_newline(lines)
    lines.append(new_line)
    return lines, False


def nonhelm_upsert_secret(lines: list, key: str, b64_value: str) -> tuple[list, bool]:
    """Add or update KEY: <b64value> in the data: section of a raw Secret."""
    new_line = f'  {key}: {b64_value}\n'
    in_data = False
    for idx, line in enumerate(lines):
        if re.match(r"^data:\s*$", line):
            in_data = True
            continue
        if in_data:
            if re.match(r"^\S", line) and not line.strip().startswith("#"):
                lines.insert(idx, new_line)
                return lines, False
            if re.match(rf"^\s+{re.escape(key)}\s*:", line):
                lines[idx] = new_line
                return lines, True
    ensure_trailing_newline(lines)
    lines.append(new_line)
    return lines, False


def nonhelm_inject_deployment(
    lines: list,
    key: str,
    app_name: str,
    ref_type: str,
    ref_name: str,
) -> tuple[list, bool]:
    """
    Inject a configMapKeyRef / secretKeyRef env entry into the named container
    inside a raw (non-Helm) Deployment yaml.
    Returns (lines, already_existed).

    Convention: the container name: field inside the backendconfig YAML must
    equal app_name exactly.
    """
    # Already there?
    if any(re.match(rf"\s+- name: {re.escape(key)}\s*$", l) for l in lines):
        return lines, True

    found_container = False
    in_env_block    = False
    insert_at       = None
    env_indent_str  = ""

    for idx, line in enumerate(lines):
        if not found_container:
            if re.search(rf"^\s*-\s*name:\s*{re.escape(app_name)}\s*$", line):
                found_container = True
            continue
        if not in_env_block:
            m = re.match(r"^(\s+)env:\s*$", line)
            if m:
                in_env_block   = True
                env_indent_str = m.group(1)
            continue
        if line.strip() == "" or line.strip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= len(env_indent_str):
            insert_at = idx
            break

    if insert_at is None and in_env_block:
        insert_at = len(lines)

    if not found_container:
        print(f"[ERROR] Container '{app_name}' not found in backendconfig.", file=sys.stderr)
        sys.exit(1)
    if not in_env_block:
        print(f"[ERROR] No 'env:' block found under container '{app_name}'.", file=sys.stderr)
        sys.exit(1)

    item_indent = env_indent_str + "  "
    new_block = (
        f"{item_indent}- name: {key}\n"
        f"{item_indent}  valueFrom:\n"
        f"{item_indent}    {ref_type}:\n"
        f"{item_indent}      name: {ref_name}\n"
        f"{item_indent}      key: {key}\n"
    )
    lines.insert(insert_at, new_block)
    return lines, False


# ──────────────────────────────────────────────────────────────────────────────
#  CSV registry
# ──────────────────────────────────────────────────────────────────────────────
def load_projects_csv(csv_path: str) -> list[dict]:
    if not os.path.exists(csv_path):
        print(f"[ERROR] {csv_path} not found.", file=sys.stderr)
        sys.exit(1)
    rows = []
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh, skipinitialspace=True):
            rows.append({
                "project":     row["project"].strip(),
                "server_name": row["server_name"].strip(),
                "directory":   row["directory"].strip(),
                "helm":        row["helm"].strip().lower() in ("true", "yes", "1"),
                "secret_file": row.get("secret_file", "").strip(),
            })
    return rows


# ──────────────────────────────────────────────────────────────────────────────
#  Overwrite decision helper
# ──────────────────────────────────────────────────────────────────────────────
def decide_overwrite(env, key, path, old_val, new_val, sensitive, global_ow) -> bool:
    masked_old = "*" * len(str(old_val)) if sensitive else str(old_val)
    masked_new = "*" * len(new_val)      if sensitive else new_val
    print(f"\n[WARN]  [{env}] '{key}' already exists in {path}")
    print(f"  Current : {masked_old}")
    print(f"  New     : {masked_new}")
    if global_ow is not None:
        print(f"  OVERWRITE={global_ow} → {'overwriting' if global_ow else 'skipping'}.")
        return global_ow
    while True:
        ans = input(f"  Overwrite '{key}' in [{env}]? (yes/no): ").strip().lower()
        if ans in ("yes", "y"):
            return True
        if ans in ("no", "n"):
            return False
        print("  Please enter yes or no.")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 0  –  Read & validate inputs
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 0 – Read inputs")

PROJECT  = os.environ.get("PROJECT",  "").strip()
KEY      = os.environ.get("KEY",      "").strip().upper()
VALUE    = os.environ.get("VALUE",    "").strip()
APP_NAME = os.environ.get("APP_NAME", "").strip()
DRY_RUN  = os.environ.get("DRY_RUN",  "true").strip().lower() in ("true", "yes", "1")

BRANCH_NAME  = os.environ.get("BRANCH_NAME",  "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "").strip()
BASE_BRANCH  = os.environ.get("BASE_BRANCH",  "main").strip()
REQUESTER    = os.environ.get("REQUESTER",    "unknown").strip()

# ── Load registry ─────────────────────────────────────────────────────────────
all_rows     = load_projects_csv(PROJECTS_CSV)
ALL_PROJECTS = sorted({r["project"] for r in all_rows})

if not PROJECT:
    print(f"[ERROR] PROJECT is required. Known: {ALL_PROJECTS}", file=sys.stderr)
    sys.exit(1)

project_rows = [r for r in all_rows if r["project"] == PROJECT]
if not project_rows:
    print(f"[ERROR] Project '{PROJECT}' not in projects.csv. Known: {ALL_PROJECTS}",
          file=sys.stderr)
    sys.exit(1)

ALL_ENVS_FOR_PROJECT = [r["server_name"] for r in project_rows]
IS_HELM              = project_rows[0]["helm"]
env_row_map          = {r["server_name"]: r for r in project_rows}

# ── ENV ───────────────────────────────────────────────────────────────────────
_env_raw = os.environ.get("ENV", "").strip().lower()
if _env_raw == "all":
    TARGET_ENVS = list(ALL_ENVS_FOR_PROJECT)
elif _env_raw:
    TARGET_ENVS = [e.strip() for e in _env_raw.split(",") if e.strip()]
else:
    TARGET_ENVS = []

# ── IS_SENSITIVE ──────────────────────────────────────────────────────────────
_sens_raw = os.environ.get("IS_SENSITIVE", "").strip().lower()
if _sens_raw == "":
    print("\n[INPUT REQUIRED] IS_SENSITIVE not set.")
    while True:
        ans = input("  Sensitive value? (yes/no): ").strip().lower()
        if ans in ("yes", "y", "true", "1"):
            IS_SENSITIVE = True; break
        elif ans in ("no", "n", "false", "0"):
            IS_SENSITIVE = False; break
        print("  Please enter yes or no.")
else:
    IS_SENSITIVE = _sens_raw in ("true", "yes", "1")

# ── OVERWRITE ────────────────────────────────────────────────────────────────
_ow_raw  = os.environ.get("OVERWRITE", "").strip().lower()
OVERWRITE: bool | None = (_ow_raw in ("true", "yes", "1") if _ow_raw else None)

# ── ADD_IF_CONDITION (Helm only) ─────────────────────────────────────────────
_if_raw = os.environ.get("ADD_IF_CONDITION", "").strip().lower()
if _if_raw in ("true", "yes", "1"):
    ADD_IF_CONDITION = True
elif _if_raw in ("false", "no", "0"):
    ADD_IF_CONDITION = False
else:
    ADD_IF_CONDITION = IS_HELM and (sorted(TARGET_ENVS) != sorted(ALL_ENVS_FOR_PROJECT))

# ── Validate ──────────────────────────────────────────────────────────────────
errors: list[str] = []
if not TARGET_ENVS:
    errors.append(f"ENV required. Valid for '{PROJECT}': {ALL_ENVS_FOR_PROJECT}")
else:
    bad = [e for e in TARGET_ENVS if e not in ALL_ENVS_FOR_PROJECT]
    if bad:
        errors.append(f"Unknown ENV(s): {bad}. Valid: {ALL_ENVS_FOR_PROJECT}")
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

# ── Pre-flight (non-dry) ──────────────────────────────────────────────────────
if not DRY_RUN:
    if not GITHUB_TOKEN:
        print("[ERROR] GITHUB_TOKEN required when DRY_RUN=false.", file=sys.stderr)
        sys.exit(1)
    if not GITHUB_REPO:
        print("[ERROR] GITHUB_REPO required when DRY_RUN=false.", file=sys.stderr)
        sys.exit(1)

# ── Derived ───────────────────────────────────────────────────────────────────
TIMESTAMP = datetime.now().strftime("%Y%m%d%H%M%S")
if not BRANCH_NAME:
    BRANCH_NAME = f"env-update/{PROJECT}/{KEY.lower().replace('_', '-')}-{TIMESTAMP}"
    print(f"[INFO]  BRANCH_NAME not set – using: {BRANCH_NAME}")

helm_key = KEY.lower().replace("-", "_")   # used for Helm .Values references

print(f"  PROJECT         : {PROJECT}  ({'Helm' if IS_HELM else 'non-Helm'})")
print(f"  ENV(s)          : {', '.join(TARGET_ENVS)}")
print(f"  KEY             : {KEY}")
print(f"  VALUE           : {'*' * len(VALUE)}  (masked)")
print(f"  SENSITIVE       : {IS_SENSITIVE}")
print(f"  APP_NAME        : {APP_NAME}")
print(f"  DRY_RUN         : {DRY_RUN}")
print(f"  BRANCH          : {BRANCH_NAME}")
print(f"  ADD_IF_CONDITION: {ADD_IF_CONDITION}  (Helm only)")
print(f"  REQUESTER       : {REQUESTER}")


# ──────────────────────────────────────────────────────────────────────────────
#  Resolve & validate file paths
# ──────────────────────────────────────────────────────────────────────────────
if IS_HELM:
    project_templates = os.path.join(PROJECT, HELM_TEMPLATES_DIR)
    helm_configmap    = os.path.join(project_templates, "configmap.yaml")
    helm_secret       = os.path.join(project_templates, "secret.yaml")
    helm_deployment   = os.path.join(project_templates, "deployment.yaml")

    missing = [f for f in [helm_configmap, helm_secret, helm_deployment]
               if not os.path.exists(f)]
    if missing:
        print(f"[ERROR] Missing Helm templates: {missing}", file=sys.stderr)
        sys.exit(1)

    CONFIGMAP_REF_NAME = extract_metadata_name(helm_configmap)
    SECRET_REF_NAME    = extract_metadata_name(helm_secret)
    print(f"[INFO]  ConfigMap ref name : {CONFIGMAP_REF_NAME}")
    print(f"[INFO]  Secret ref name    : {SECRET_REF_NAME}")

    def get_values_path(env: str) -> str:
        return os.path.join(env_row_map[env]["directory"], f"{env}-values.yaml")

else:
    # non-Helm: resolve configmap, secret, and backendconfig per env
    def get_nonhelm_configmap_path(env: str) -> str:
        return os.path.join(env_row_map[env]["directory"], NON_HELM_CONFIGMAP_FILENAME)

    def get_nonhelm_secret_path(env: str) -> str:
        secret_file = env_row_map[env]["secret_file"]
        if not secret_file:
            print(f"[ERROR] 'secret_file' column is blank in projects.csv for "
                  f"project='{PROJECT}' env='{env}'", file=sys.stderr)
            sys.exit(1)
        return os.path.join(env_row_map[env]["directory"], secret_file)

    def get_nonhelm_backendconfig_path(env: str) -> str:
        return os.path.join(
            env_row_map[env]["directory"],
            f"{PROJECT}-{APP_NAME}-backendconfig.yaml",
        )

    # Validate all target env files exist
    for env in TARGET_ENVS:
        for p in [
            get_nonhelm_configmap_path(env),
            get_nonhelm_secret_path(env),
            get_nonhelm_backendconfig_path(env),
        ]:
            if not os.path.exists(p):
                print(f"[ERROR] Missing file: {p}", file=sys.stderr)
                sys.exit(1)

    # FIX: read ref names per env (inside loop) to handle envs with different
    # metadata names.  We pre-resolve them here into a dict for STEP 4.
    nonhelm_configmap_ref = {}
    nonhelm_secret_ref    = {}
    for env in TARGET_ENVS:
        nonhelm_configmap_ref[env] = extract_metadata_name(get_nonhelm_configmap_path(env))
        nonhelm_secret_ref[env]    = extract_metadata_name(get_nonhelm_secret_path(env))
        print(f"[INFO]  [{env}] ConfigMap ref name : {nonhelm_configmap_ref[env]}")
        print(f"[INFO]  [{env}] Secret ref name    : {nonhelm_secret_ref[env]}")

    # Validate that APP_NAME matches a container name in each backendconfig
    for env in TARGET_ENVS:
        bc_path = get_nonhelm_backendconfig_path(env)
        bc_lines = read_lines(bc_path)
        if not any(re.search(rf"^\s*-\s*name:\s*{re.escape(APP_NAME)}\s*$", l) for l in bc_lines):
            print(f"[ERROR] Container '{APP_NAME}' not found in {bc_path}. "
                  f"APP_NAME must match the container name: field exactly.", file=sys.stderr)
            sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 1  –  Existence check
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 1 – Existence check")

if IS_HELM:
    cm_lines  = read_lines(helm_configmap)
    sec_lines = read_lines(helm_secret)
    key_in_cm  = any(re.match(rf"^\s+{re.escape(KEY)}\s*:", l) for l in cm_lines)
    key_in_sec = any(re.match(rf"^\s+{re.escape(KEY)}\s*:", l) for l in sec_lines)
    if key_in_cm or key_in_sec:
        loc = "configmap.yaml" if key_in_cm else "secret.yaml"
        ref = get_existing_indented_value(cm_lines if key_in_cm else sec_lines, KEY)
        print(f"[INFO]  '{KEY}' exists in templates/{loc}  (ref: {ref})")
        print(f"        Per-env values checked individually in STEP 3.")
    else:
        print(f"[OK]    '{KEY}' is new in Helm templates.")
        key_in_cm = key_in_sec = False
else:
    print("[INFO]  non-Helm: per-env configmap/secret checked per env in STEP 2.")
    key_in_cm = key_in_sec = False


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 2  –  Update configmap / secret
#
#  Helm     → shared templates/configmap.yaml or templates/secret.yaml
#  non-Helm → per-env configmap.yaml or <project>-secret.yaml  (hardcoded values)
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 2 – Update configmap / secret")

CHANGED_FILES: set[str] = set()

if IS_HELM:
    cm_lines  = read_lines(helm_configmap)
    sec_lines = read_lines(helm_secret)

    if IS_SENSITIVE:
        # FIX: apply ADD_IF_CONDITION wrapping to secret entries too
        helm_secret_ref = f"{{{{ .Values.{helm_key} | b64enc }}}}"
        if key_in_cm:
            write_lines(helm_configmap, remove_key_block_from_lines(cm_lines, KEY))
            print(f"[OK]    Removed '{KEY}' from configmap.yaml (moved to secret).")
            cm_lines = read_lines(helm_configmap)

        if ADD_IF_CONDITION:
            new_block = (
                f"  {{{{- if .Values.{helm_key} }}}}\n"
                f"  {KEY}: {helm_secret_ref}\n"
                f"  {{{{- end }}}}\n"
            )
            sec_lines, replaced = upsert_indented_block(sec_lines, KEY, new_block)
            suffix = " (if-wrapped)"
        else:
            new_line = f"  {KEY}: {helm_secret_ref}\n"
            sec_lines, replaced = upsert_indented_line(sec_lines, KEY, new_line)
            suffix = ""

        write_lines(helm_secret, sec_lines)
        print(f"[OK]    {'Updated' if replaced else 'Added'} '{KEY}' in secret.yaml{suffix}")
        CHANGED_FILES.update([helm_configmap, helm_secret])

    else:
        helm_ref = f"{{{{ .Values.{helm_key} }}}}"
        if key_in_sec:
            write_lines(helm_secret, remove_key_block_from_lines(sec_lines, KEY))
            print(f"[OK]    Removed '{KEY}' from secret.yaml (moved to configmap).")
            sec_lines = read_lines(helm_secret)

        if ADD_IF_CONDITION:
            new_block = (
                f"  {{{{- if .Values.{helm_key} }}}}\n"
                f'  {KEY}: "{helm_ref}"\n'
                f"  {{{{- end }}}}\n"
            )
            cm_lines, replaced = upsert_indented_block(cm_lines, KEY, new_block)
        else:
            new_line  = f'  {KEY}: "{helm_ref}"\n'
            cm_lines, replaced = upsert_indented_line(cm_lines, KEY, new_line)

        write_lines(helm_configmap, cm_lines)
        suffix = " (if-wrapped)" if ADD_IF_CONDITION else ""
        print(f"[OK]    {'Updated' if replaced else 'Added'} '{KEY}' in configmap.yaml{suffix}")
        CHANGED_FILES.update([helm_configmap, helm_secret])

else:
    # non-Helm: update configmap.yaml OR secret file per targeted env
    for env in TARGET_ENVS:
        if IS_SENSITIVE:
            secret_path = get_nonhelm_secret_path(env)
            sec_lines   = read_lines(secret_path)

            # FIX: use dedicated data-section-aware check (not raw regex)
            key_exists = nonhelm_key_in_secret(sec_lines, KEY)
            if key_exists:
                old_val = get_existing_indented_value(sec_lines, KEY) or ""
                if not decide_overwrite(env, KEY, secret_path, old_val,
                                        VALUE, IS_SENSITIVE, OVERWRITE):
                    print(f"[INFO]  [{env}] Skipped secret — keeping existing.")
                    continue

            b64_value = base64.b64encode(VALUE.encode()).decode()
            sec_lines, replaced = nonhelm_upsert_secret(sec_lines, KEY, b64_value)
            write_lines(secret_path, sec_lines)
            print(f"[OK]    [{env}] {'Updated' if replaced else 'Added'} "
                  f"'{KEY}' in {secret_path}  (b64 encoded, unquoted)")
            CHANGED_FILES.add(secret_path)

        else:
            cm_path  = get_nonhelm_configmap_path(env)
            cm_lines = read_lines(cm_path)

            key_exists = nonhelm_key_in_configmap(cm_lines, KEY)
            if key_exists:
                old_val = get_existing_indented_value(cm_lines, KEY) or ""
                if not decide_overwrite(env, KEY, cm_path, old_val,
                                        VALUE, IS_SENSITIVE, OVERWRITE):
                    print(f"[INFO]  [{env}] Skipped configmap — keeping existing.")
                    continue

            cm_lines, replaced = nonhelm_upsert_configmap(cm_lines, KEY, VALUE)
            write_lines(cm_path, cm_lines)
            print(f"[OK]    [{env}] {'Updated' if replaced else 'Added'} "
                  f"'{KEY}: {VALUE}' in {cm_path}")
            CHANGED_FILES.add(cm_path)


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 3  –  Per-env values files  (Helm only)
# ──────────────────────────────────────────────────────────────────────────────
if IS_HELM:
    banner("STEP 3 – Update Helm per-env values files")

    for env in TARGET_ENVS:
        values_path = get_values_path(env)
        y, data     = load_yaml_file(values_path)
        old         = data.get(helm_key)

        if old is not None:
            if not decide_overwrite(env, helm_key, values_path, old,
                                    VALUE, IS_SENSITIVE, OVERWRITE):
                print(f"[INFO]  [{env}] Skipped values — keeping existing.")
                continue

        data[helm_key] = VALUE
        dump_yaml_file(y, data, values_path)
        masked = "*" * len(VALUE) if IS_SENSITIVE else VALUE
        action = f"Updated (was: {old!r})" if old is not None else "Added"
        print(f"[OK]    [{env}] {action} '{helm_key}: {masked}' in {values_path}")
        CHANGED_FILES.add(values_path)

    skipped = [e for e in ALL_ENVS_FOR_PROJECT if e not in TARGET_ENVS]
    if skipped:
        print(f"[INFO]  Key NOT added to: {', '.join(skipped)}  "
              f"(ADD_IF_CONDITION={'yes' if ADD_IF_CONDITION else 'no'})")
else:
    banner("STEP 3 – Skipped (non-Helm: no values files)")


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 4  –  Patch deployment
#
#  Helm     → templates/deployment.yaml  (Helm refs, optional if-condition)
#  non-Helm → <project>-<APP_NAME>-backendconfig.yaml  (hardcoded refs, per env)
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 4 – Patch deployment")

ref_type = "secretKeyRef" if IS_SENSITIVE else "configMapKeyRef"

if IS_HELM:
    raw = read_text(helm_deployment)
    if f"name: {KEY}" in raw:
        print(f"[WARN]  '{KEY}' already in deployment.yaml – skipping.")
    else:
        ref_name = SECRET_REF_NAME if IS_SENSITIVE else CONFIGMAP_REF_NAME
        lines    = raw.splitlines(keepends=True)

        found_container = in_env_block = False
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
                    in_env_block   = True
                    env_indent_str = m.group(1)
                continue
            if line.strip() == "" or line.strip().startswith("#"):
                continue
            if len(line) - len(line.lstrip()) <= len(env_indent_str):
                insert_at = idx
                break

        if insert_at is None and in_env_block:
            insert_at = len(lines)

        if not found_container:
            print(f"[ERROR] Container '{APP_NAME}' not found in deployment.yaml.",
                  file=sys.stderr)
            sys.exit(1)
        if not in_env_block:
            print(f"[ERROR] No 'env:' block under '{APP_NAME}'.", file=sys.stderr)
            sys.exit(1)

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
        write_text(helm_deployment, "".join(lines))
        if_note = " (if-wrapped)" if ADD_IF_CONDITION else ""
        print(f"[OK]    Injected '{KEY}' into '{APP_NAME}' in deployment.yaml{if_note}")
        CHANGED_FILES.add(helm_deployment)

else:
    # non-Helm: patch each env's backendconfig
    # FIX: ref names are resolved per-env from the pre-built dicts
    for env in TARGET_ENVS:
        bc_path  = get_nonhelm_backendconfig_path(env)
        ref_name = (
            nonhelm_secret_ref[env]
            if IS_SENSITIVE
            else nonhelm_configmap_ref[env]
        )
        lines = read_lines(bc_path)
        lines, already = nonhelm_inject_deployment(lines, KEY, APP_NAME, ref_type, ref_name)

        if already:
            print(f"[WARN]  [{env}] '{KEY}' already in {bc_path} – skipping.")
        else:
            write_lines(bc_path, lines)
            print(f"[OK]    [{env}] Injected '{KEY}' into '{APP_NAME}' in {bc_path}")
            CHANGED_FILES.add(bc_path)


# ──────────────────────────────────────────────────────────────────────────────
#  STEP 5  –  Git commit, push & raise PR
# ──────────────────────────────────────────────────────────────────────────────
banner("STEP 5 – Git commit, push & raise PR")

envs_label = ", ".join(TARGET_ENVS)
helm_note  = "Helm" if IS_HELM else "non-Helm"
# Convert set to sorted list for deterministic git add output
changed_files_list = sorted(CHANGED_FILES)

if DRY_RUN:
    print("[DRY_RUN] Skipping git operations.")
    print(f"[DRY_RUN] Would create branch : {BRANCH_NAME}")
    print(f"[DRY_RUN] Would stage         : {', '.join(changed_files_list)}")
    print(f"[DRY_RUN] Would commit        : "
          f"chore(env): [{PROJECT}] add/update '{KEY}' [{envs_label}]")
    print(f"[DRY_RUN] Would push to       : origin/{BRANCH_NAME}")
    print(f"[DRY_RUN] Would raise PR      : {BRANCH_NAME} → {BASE_BRANCH}")
else:
    commit_msg = (
        f"chore(env): [{PROJECT}] add/update '{KEY}' [{envs_label}]\n\n"
        f"Project        : {PROJECT}  ({helm_note})\n"
        f"Key            : {KEY}\n"
        f"Sensitive      : {IS_SENSITIVE}\n"
        f"App            : {APP_NAME}\n"
        f"Envs updated   : {envs_label}\n"
        f"If-condition   : {ADD_IF_CONDITION}\n"
        f"Branch         : {BRANCH_NAME}"
    )

    run_git(["git", "checkout", "-b", BRANCH_NAME])
    run_git(["git", "add"] + changed_files_list)

    result = subprocess.run(["git", "commit", "-m", commit_msg],
                            capture_output=True, text=True)
    if result.returncode != 0 and "nothing to commit" not in result.stdout.lower():
        print(f"[ERROR] git commit failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    run_git(["git", "push", "-u", "origin", BRANCH_NAME])
    print(f"[OK]    Branch '{BRANCH_NAME}' pushed.")

    pr_title = f"chore(env): [{PROJECT}] add/update '{KEY}' [{envs_label}]"
    pr_body  = "\n".join([
        "## Env Variable Update",
        "",
        "| Field           | Value |",
        "|-----------------|-------|",
        f"| Project         | `{PROJECT}` |",
        f"| Type            | `{helm_note}` |",
        f"| Key             | `{KEY}` |",
        f"| Sensitive       | `{IS_SENSITIVE}` |",
        f"| App             | `{APP_NAME}` |",
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
        url     = f"https://api.github.com/repos/{GITHUB_REPO}/pulls",
        data    = payload,
        method  = "POST",
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
        print(f"[ERROR] PR creation failed ({e.code}): {e.read().decode()}",
              file=sys.stderr)
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
banner("Done ✓")
print(f"  Project        : {PROJECT}  ({helm_note})")
print(f"  Key            : {KEY}")
print(f"  Sensitive      : {IS_SENSITIVE}")
print(f"  App            : {APP_NAME}")
print(f"  Envs updated   : {envs_label}")
print(f"  If-condition   : {ADD_IF_CONDITION}")
print(f"  Requester      : {REQUESTER}")
print(f"  Branch         : {BRANCH_NAME}  "
      f"{'(not pushed – dry-run)' if DRY_RUN else '(pushed + PR raised)'}")
print()