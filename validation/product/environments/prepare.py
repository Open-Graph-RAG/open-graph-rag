from __future__ import annotations
import hashlib, json, shutil
from pathlib import Path

FORBIDDEN={".env",".env.local",".env.production","compose.override.yaml","compose.override.yml","credentials.json"}
def _hash(p: Path): return hashlib.sha256(p.read_bytes()).hexdigest()
def prepare_snapshot(source: Path, destination: Path) -> dict:
    """Clone a pre-sanitized, manifest-pinned LightRAG snapshot for isolated A/B.

    Only files listed in snapshot-manifest.json are copied. No deployment checkout,
    shared index, Compose command, service startup, or host port is touched.
    """
    source=source.resolve(strict=True); destination=destination.resolve()
    if not source.is_dir() or source==destination or source in destination.parents or destination in source.parents: raise ValueError("source and destination must be disjoint directories")
    manifest_path=source/"snapshot-manifest.json"
    if not manifest_path.is_file(): raise ValueError("pre-existing snapshot-manifest.json required")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version")!=1 or manifest.get("sanitized") is not True or not isinstance(manifest.get("files"),dict) or not manifest["files"]: raise ValueError("snapshot must have a valid sanitized manifest")
    files={}
    for rel,expected in manifest["files"].items():
        rp=Path(rel)
        if rp.is_absolute() or ".." in rp.parts or any(part in FORBIDDEN for part in rp.parts): raise ValueError("snapshot manifest contains unsafe path")
        src=(source/rp).resolve()
        if source not in src.parents or not src.is_file() or src.is_symlink(): raise ValueError("snapshot file missing or unsafe")
        if _hash(src)!=expected: raise ValueError(f"snapshot hash mismatch: {rel}")
        files[rel]=src
    for path in source.rglob("*"):
        if path.is_symlink(): raise ValueError("snapshot symlinks are refused")
        if path.name in FORBIDDEN: raise ValueError("secret or deployment override present in snapshot")
    if destination.exists(): raise FileExistsError("destination exists; refusing overwrite")
    destination.mkdir(parents=True,mode=0o700)
    destination.chmod(0o700)
    arms={}
    try:
        for arm in ("baseline","candidate"):
            target=destination/arm
            target.mkdir(mode=0o700)
            target.chmod(0o700)
            for rel,src in files.items():
                dst=target/rel; dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                for parent in (dst.parent,*dst.parent.parents):
                    if parent==target: break
                    if target not in parent.parents: continue
                    parent.chmod(0o700)
                shutil.copyfile(src,dst); dst.chmod(0o600)
            shutil.copyfile(manifest_path,target/manifest_path.name)
            (target/manifest_path.name).chmod(0o600)
            arms[arm]={"path":str(target),"files_sha256":{rel:_hash(target/rel) for rel in files}}
        if arms["baseline"]["files_sha256"]!=arms["candidate"]["files_sha256"]: raise RuntimeError("A/B snapshot clone hashes differ")
    except Exception:
        shutil.rmtree(destination,ignore_errors=True); raise
    return {"snapshot_id":manifest.get("snapshot_id"),"arms":arms,"baseline_candidate_content_identical":True,"services_started":False,"shared_deployment_modified":False,"host_ports_exposed":False,"source_snapshot_modified":False}
