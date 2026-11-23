import subprocess
import sys
import os
import json

def walk_all(round_id="R800"):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for step in range(0, 12):
        print(f"=== Starting Step {step} for {round_id} ===")
        while True:
            proc = subprocess.run(
                [sys.executable, "scripts/agent_wake_verification.py", "--round", round_id, "--step", str(step)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env
            )
            if proc.returncode != 0:
                print(f"Step {step} error (returncode {proc.returncode}): {proc.stderr.strip()}")
                return False
            
            remaining = None
            read_keys = []
            for line in proc.stdout.splitlines():
                if "WAKE_RECEIPT " in line:
                    data = json.loads(line.replace("WAKE_RECEIPT ", ""))
                    remaining = data.get("remaining_anchors", [])
                    read_keys = list(data.get("sections_read", {}).keys())
            
            print(f"Step {step}: read {read_keys}, remaining: {remaining}")
            if not remaining:
                break
    return True

if __name__ == "__main__":
    r_id = sys.argv[1] if len(sys.argv) > 1 else "R800"
    ok = walk_all(r_id)
    if ok:
        print(f"All Steps 0-11 successfully read for {r_id}!")
