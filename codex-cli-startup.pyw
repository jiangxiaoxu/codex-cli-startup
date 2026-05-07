import runpy
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().with_name("codex-cli-startup.py")

if __name__ == "__main__":
    runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
