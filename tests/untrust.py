# Removes ~/.claude.json's entries for the tests' throwaway folders (~/.cache/fleet-test-*), which
# Fleet marked trusted when it started Claude agents there. run.sh runs it on both machines.
import json, os
path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~"), ".claude.json")
try:
    with open(path) as f:
        config = json.load(f)
except (OSError, ValueError):
    raise SystemExit
gone = [k for k in config.get("projects", {}) if "/.cache/fleet-test-" in k]
if gone:
    for k in gone:
        del config["projects"][k]
    tmp = path + ".fleet-test"
    with open(tmp, "w") as f:
        f.write(json.dumps(config, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
