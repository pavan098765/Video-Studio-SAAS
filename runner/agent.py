"""Pull-agent: HMAC heartbeat against genedit, run loop.py, report events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

from runner import r2
from runner.config import env, load_saas, repo_root
from runner.loop import run_job


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(url: str, payload: dict, secret: str) -> dict:
    raw = json.dumps(payload).encode()
    req = Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Worker-HMAC", _sign(secret, raw))
    req.add_header("Authorization", f"Bearer {secret}")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def _upload_key(base: str, secret: str, key: str, path: Path, content_type: str) -> str | None:
    if not path.is_file():
        return None
    try:
        signed = _post(f"{base}/studio/internal/presign", {"key": key, "method": "put"}, secret)
        url = signed.get("url")
        if not url:
            return None
        r2.put_file(url, path, content_type)
        return key
    except Exception:
        return None


def _publish_storyboard(base: str, secret: str, job: dict, storyboard: dict | None) -> dict | None:
    if not storyboard:
        return storyboard
    prefix = (job.get("r2") or {}).get("prefix") or f"studio/{job.get('job_id')}"
    for scene in storyboard.get("scenes") or []:
        sid = scene.get("id") or "scene"
        preview = scene.get("preview")
        poster = scene.get("poster")
        if preview:
            key = _upload_key(base, secret, f"{prefix}/scenes/{sid}.mp4", Path(preview), "video/mp4")
            if key:
                scene["preview_key"] = key
        if poster:
            key = _upload_key(base, secret, f"{prefix}/posters/{sid}.png", Path(poster), "image/png")
            if key:
                scene["poster_key"] = key
    return storyboard


def main() -> None:
    load_saas()
    base = env("GENEDIT_URL", "https://genedit.mooo.com").rstrip("/")
    secret = env("WORKER_SECRET") or env("WORKER_HMAC_SECRET")
    if not secret:
        raise SystemExit("WORKER_SECRET required")
    worker = {
        "model": "studio_engine",
        "worker_kind": "studio",
        "chrome_slots": int(env("CHROME_SLOTS", "1")),
        "ram_free_mb": 8192,
        "cpu_free": 4,
        "vultr_id": env("VULTR_INSTANCE_ID", ""),
    }
    try:
        _post(f"{base}/studio/internal/heartbeat", {**worker, "worker_id": "studio-engine"}, secret)
    except urllib.error.URLError:
        pass
    while True:
        try:
            _post(f"{base}/studio/internal/heartbeat", {**worker, "worker_id": "studio-engine"}, secret)
        except urllib.error.URLError:
            time.sleep(5)
            continue
        job = None
        try:
            req = Request(f"{base}/studio/internal/next-job")
            req.add_header("Authorization", f"Bearer {secret}")
            with urlopen(req, timeout=30) as resp:
                job = json.loads(resp.read().decode())
                if not job.get("job_id"):
                    job = None
        except urllib.error.URLError:
            job = None
        if not job:
            time.sleep(3)
            continue
        work = Path(env("WORK_DIR", str(repo_root() / "work"))) / job["job_id"]
        result = run_job(job, work_dir=work)
        status = result.get("status")
        extra = {
            "scene_runtimes": result.get("scene_runtimes"),
            "compose_strategy": result.get("compose_strategy"),
        }
        if status == "review":
            board = _publish_storyboard(base, secret, job, result.get("storyboard"))
            _post(
                f"{base}/studio/internal/review_ready",
                {"job_id": job["job_id"], "storyboard": board, **extra},
                secret,
            )
        elif status == "done":
            prefix = (job.get("r2") or {}).get("prefix") or f"studio/{job.get('job_id')}"
            final_key = f"{prefix}/final.mp4"
            final_path = result.get("final_mp4")
            if final_path:
                _upload_key(base, secret, final_key, Path(final_path), "video/mp4")
            _publish_storyboard(base, secret, job, result.get("storyboard"))
            _post(
                f"{base}/studio/internal/complete",
                {
                    "job_id": job["job_id"],
                    "result_r2_key": final_key,
                    "final_mp4": result.get("final_mp4"),
                    **extra,
                },
                secret,
            )
        else:
            _post(
                f"{base}/studio/internal/failed",
                {"job_id": job["job_id"], "error": result.get("error")},
                secret,
            )


if __name__ == "__main__":
    main()
