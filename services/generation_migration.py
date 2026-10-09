from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from services.generation_store import dump


def import_legacy_tasks(store, path: Path) -> None:
    # The original JSON is never opened for writing.
    source = json.loads(path.read_text(encoding="utf-8"))
    items = source.get("tasks", []) if isinstance(source, dict) else source
    if not isinstance(items, list):
        return
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM metadata WHERE name='legacy_imported'").fetchone():
            return
        for item in items:
            if not isinstance(item, dict) or not item.get("id") or not item.get("owner_id"):
                continue
            job_id, child_id = uuid.uuid4().hex, uuid.uuid4().hex
            now = float(item.get("created_ts") or time.time())
            owner = str(item["owner_id"])
            status = item.get("status") if item.get("status") in {"success", "error"} else "error"
            error = item.get("error") or ("服务已重启，未完成的图片任务已中断" if item.get("status") not in {"success", "error"} else "")
            db.execute("INSERT INTO jobs(id,owner,identity,endpoint,external_id,fingerprint,payload,model,channel,created,deadline,legacy) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                       (job_id, owner, dump({"id": owner}), "/api/image-tasks/" + ("edits" if item.get("mode") == "edit" else "generations"), item["id"], "legacy", dump({"size": item.get("size"), "quality": item.get("quality")}), item.get("model") or "gpt-image-2", "web", now, now))
            outputs = [{"kind": "result", "data": item.get("data") or []}] if status == "success" else []
            db.execute("INSERT INTO tasks(id,job_id,ordinal,owner,status,phase,created,updated,finished,reservation,output,error) VALUES (?,?,1,?,?,?,?,?,?,0,?,?)",
                       (child_id, job_id, owner, status, status, now, now, now, dump(outputs), error))
        # Commit the marker with imported rows so a crash cannot duplicate history.
        db.execute("INSERT OR IGNORE INTO metadata VALUES('legacy_imported','1')")
