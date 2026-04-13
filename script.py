import yaml
import base64
import os
import subprocess
import sys

print("Script started")

# -------- READ ENV VARIABLES --------
env_name = os.environ.get("ENV")
key = os.environ.get("KEY")
value = os.environ.get("VALUE")
is_sensitive = os.environ.get("IS_SENSITIVE", "false").lower() == "true"
app_name = os.environ.get("APP_NAME")
branch_name = os.environ.get("BRANCH_NAME")

config_file = f"{env_name}/configmap.yaml"
secret_file = f"{env_name}/secret.yaml"
deployment_file = f"{env_name}/deployment.yaml"

# -------- HELPERS --------
def load_yaml(file):
    if not os.path.exists(file):
        return {}
    with open(file) as f:
        return yaml.safe_load(f) or {}

def save_yaml(file, data):
    with open(file, "w") as f:
        yaml.dump(data, f, default_flow_style=False)

def git_commit_push(branch_name, message):
    try:
        subprocess.run(["git", "checkout", "-b", branch_name], check=True)
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push", "-u", "origin", branch_name], check=True)
        print("Changes pushed to:", branch_name)
    except subprocess.CalledProcessError as e:
        print("Git error:", e)
        sys.exit(1)

# -------- VALIDATION --------
if not all([env_name, key, value, app_name, branch_name]):
    print("Missing required environment variables")
    sys.exit(1)

# -------- CONFIGMAP / SECRET UPDATE --------
target_file = secret_file if is_sensitive else config_file
data = load_yaml(target_file)

data.setdefault("data", {})

# Encode if sensitive
if is_sensitive:
    encoded_value = base64.b64encode(value.encode()).decode()
    data["data"][key] = encoded_value
else:
    data["data"][key] = value

save_yaml(target_file, data)
print("Updated:", target_file)

# -------- LOAD DEPLOYMENT --------
deployment = load_yaml(deployment_file)

# -------- FIND CONTAINERS --------
def find_containers(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "containers":
                return v
            result = find_containers(v)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_containers(item)
            if result:
                return result
    return None

containers = find_containers(deployment)

if not containers:
    print("Containers not found")
    sys.exit(1)

# -------- UPDATE TARGET CONTAINER --------
found = False

for container in containers:
    if container.get("name") == app_name:
        found = True

        env_list = container.setdefault("env", [])

        exists = any(e.get("name") == key for e in env_list)

        if not exists:
            if is_sensitive:
                ref = {
                    "name": key,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "cscs-secret",
                            "key": key
                        }
                    }
                }
            else:
                ref = {
                    "name": key,
                    "valueFrom": {
                        "configMapKeyRef": {
                            "name": "cscs-config",
                            "key": key
                        }
                    }
                }

            env_list.append(ref)

if not found:
    print("App not found in deployment")
    sys.exit(1)

# -------- SAVE DEPLOYMENT --------
save_yaml(deployment_file, deployment)
print("Updated deployment.yaml")

# -------- GIT COMMIT --------
commit_msg = f"Added env {key} to {app_name}"
git_commit_push(branch_name, commit_msg)

print("Done")