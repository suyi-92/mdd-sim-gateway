"""Authenticated, bounded transfer of immutable managed backups."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from . import operations

router = APIRouter()
MAX_BYTES = 1024 ** 3
_transfer_lock = threading.Lock()
_helper = Path(__file__).resolve().parents[2] / "scripts" / "mdd_archive.py"


def _run(*arguments: str) -> str:
    try:
        result = subprocess.run([sys.executable, str(_helper), *arguments],
                                capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, "backup.transfer.failed") from exc
    if result.returncode:
        raise HTTPException(400, "backup.transfer.invalid")
    return result.stdout.strip()


def _begin() -> Path:
    if not _transfer_lock.acquire(blocking=False):
        raise HTTPException(409, "backup.transfer.busy")
    try:
        root = operations._managed_directory("MDD_BACKUP_DIR", "/var/backups/mdd-sim-gateway")
        return Path(tempfile.mkdtemp(prefix=".mdd-transfer-", dir=root))
    except BaseException:
        _transfer_lock.release()
        raise


def _finish(stage: Path) -> None:
    try:
        shutil.rmtree(stage)
    finally:
        _transfer_lock.release()


class TransferResponse(FileResponse):
    """Release private staging even when the browser disconnects during download."""
    def __init__(self, *args, stage: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage = stage

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            _finish(self.stage)


@router.get("/api/system/backups/{backup_name}/export")
def export_backup(backup_name: str):
    stage = None
    try:
        archive, _metadata = operations._backup_pair(backup_name)
        stage = _begin()
        if shutil.disk_usage(stage).free < _metadata.st_size + 1024 ** 3:
            raise HTTPException(503, "backup.transfer.failed")
        output = stage / "backup.mddbackup"
        _run("export-bundle", str(archive), str(output))
        return TransferResponse(output, media_type="application/octet-stream", stage=stage,
                            filename=backup_name.removesuffix(".tar.gz") + ".mddbackup",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except BaseException as exc:
        if stage is not None:
            _finish(stage)
        if isinstance(exc, (OSError, RuntimeError, ValueError)):
            raise HTTPException(400, "backup.transfer.invalid") from exc
        raise


@router.post("/api/system/backups/import")
async def import_backup(request: Request):
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise HTTPException(415, "backup.transfer.invalid")
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError as exc:
        raise HTTPException(400, "backup.transfer.invalid") from exc
    if length < 0 or length > MAX_BYTES:
        raise HTTPException(413, "backup.transfer.too_large")
    stage = None
    try:
        stage = _begin()
        if shutil.disk_usage(stage).free < 2 * (length or MAX_BYTES) + 1024 ** 3:
            raise HTTPException(503, "backup.transfer.failed")
        source = stage / "upload.mddbackup"
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        total = 0
        async with asyncio.timeout(300):
            with os.fdopen(descriptor, "wb") as target:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, "backup.transfer.too_large")
                    await run_in_threadpool(target.write, chunk)
        # The helper validates SHA-256, archive paths, manifest and SQLite before publication.
        name = await run_in_threadpool(_run, "import-bundle", str(source), str(stage.parent),
                                      "--staging-root", str(stage))
        if not operations._BACKUP_NAME.fullmatch(name):
            raise HTTPException(503, "backup.transfer.failed")
        return {"ok": True, "backup_name": name}
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        raise HTTPException(400, "backup.transfer.invalid") from exc
    finally:
        if stage is not None:
            _finish(stage)
