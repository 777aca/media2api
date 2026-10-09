"""Run the optional worker; logs always go to logs/super-resolution.log."""
from pathlib import Path
import logging
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=ROOT / "logs/super-resolution.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("services.super_resolution_worker:app", host=os.environ.get("SUPER_RESOLUTION_BIND", "127.0.0.1"), port=int(os.environ.get("SUPER_RESOLUTION_PORT", "3310")), log_config=None)
