"""Persistent, hash-verified storage for real validation run evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import tomllib
from typing import Any, Iterable, Optional
import uuid


MANIFEST_SCHEMA = "hermes_validation_artifact_manifest_v1"
DEFAULT_ARTIFACT_ROOT = Path("/root/hermes-validation-artifacts")
RAW_ENTRY_ALLOWLIST = {
    "codex-home",
    "codex-homes",
    "worker-events",
    "provider-trace",
    "runtime-state",
    "service",
    "hermes-home",
    "evaluator",
    "evaluator-runs",
    "logs",
    "reports",
    "runtime-contributions",
    "runner-state.json",
}
REBUILDABLE_ENTRY_ALLOWLIST = {
    "workspace",
    "home",
    "codex-home-seed",
    "toolchain",
    "cache",
    "caches",
    "runtime-worktrees",
}


class ArtifactArchiveError(RuntimeError):
    pass


def default_artifact_root() -> Path:
    configured = os.environ.get("HERMES_VALIDATION_ARTIFACT_ROOT")
    return Path(configured).expanduser() if configured else DEFAULT_ARTIFACT_ROOT


def model_source_redactions(source_codex_home: Optional[Path]) -> dict[str, str]:
    """Return exact credential values that must not enter an exported archive."""

    if source_codex_home is None:
        return {}
    source = source_codex_home.expanduser().resolve()
    redactions: dict[str, str] = {}
    auth_path = source / "auth.json"
    if auth_path.is_file():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            auth = {}
        for value in _string_values(auth):
            if value:
                redactions[value] = "<redacted-model-source-key>"
    config_path = source / "config.toml"
    if config_path.is_file():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            config = {}
        provider_name = config.get("model_provider")
        providers = config.get("model_providers")
        provider = providers.get(provider_name) if isinstance(providers, dict) else None
        if isinstance(provider, dict):
            base_url = str(provider.get("base_url") or "").strip()
            if base_url:
                redactions[base_url] = "<redacted-model-source-base-url>"
                redactions[base_url.rstrip("/")] = "<redacted-model-source-base-url>"
    return redactions


def archive_validation_run(
    run_root: Path,
    *,
    artifact_root: Optional[Path],
    phase: str,
    instance_id: str,
    redactions: Optional[dict[str, str]] = None,
    expected_entries: Iterable[str] = (),
) -> dict[str, Any]:
    """Atomically copy raw evidence, redact credentials, and verify every file."""

    source = run_root.expanduser().resolve()
    if not source.is_dir():
        raise ArtifactArchiveError(f"validation run root does not exist: {source}")
    run_id = source.name
    root = (artifact_root or default_artifact_root()).expanduser().resolve()
    destination = root / str(phase) / str(instance_id) / run_id
    if destination.exists():
        manifest = verify_artifact_manifest(destination / "manifest.json")
        if manifest.get("source_run_root") != str(source):
            raise ArtifactArchiveError("existing artifact destination belongs to another source run")
        return manifest
    if destination == source or source in destination.parents:
        raise ArtifactArchiveError("artifact destination must not be inside the source run")

    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{run_id}.staging-{uuid.uuid4().hex[:10]}"
    staging.mkdir(mode=0o700)
    selected = sorted(
        child.name for child in source.iterdir()
        if child.name in RAW_ENTRY_ALLOWLIST
    )
    missing = sorted(set(str(value) for value in expected_entries) - set(selected))
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise ArtifactArchiveError(f"required raw evidence entries are missing: {missing}")

    effective_redactions = dict(redactions or {})
    effective_redactions.update(_run_auth_redactions(source))
    omitted: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}
    try:
        for name in selected:
            original = source / name
            target = staging / name
            if original.is_dir() and not original.is_symlink():
                shutil.copytree(original, target, symlinks=True)
            elif original.is_symlink():
                target.symlink_to(os.readlink(original))
            else:
                shutil.copy2(original, target)
            for file_path in _regular_files(original):
                relative = str(file_path.relative_to(source))
                source_hashes[relative] = _sha256_file(file_path)

        for auth_path in sorted(staging.rglob("auth.json")):
            omitted.append({
                "path": str(auth_path.relative_to(staging)),
                "reason": "model_source_api_key",
            })
            auth_path.unlink()
        redaction_counts = _redact_archive_files(staging, effective_redactions)
        files = _file_manifest(staging, source_hashes, redaction_counts)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "phase": str(phase),
            "instance_id": str(instance_id),
            "run_id": run_id,
            "source_run_root": str(source),
            "artifact_path": str(destination),
            "status": "verified",
            "created_at": int(time.time()),
            "selected_entries": selected,
            "missing_expected_entries": [],
            "omitted_files": omitted,
            "redaction_policy": "model_source_key_and_base_url_only",
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(int(item["bytes"]) for item in files),
        }
        _write_json(staging / "manifest.json", manifest)
        _write_catalog(staging / "ARTIFACTS.md", manifest)
        _secure_tree(staging)
        _verify_manifest_payload(staging, manifest)
        os.replace(staging, destination)
        return verify_artifact_manifest(destination / "manifest.json")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_artifact_manifest(manifest_path: Path) -> dict[str, Any]:
    path = manifest_path.expanduser().resolve()
    if not path.is_file():
        raise ArtifactArchiveError(f"artifact manifest is missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactArchiveError("artifact manifest is unreadable") from exc
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("status") != "verified":
        raise ArtifactArchiveError("artifact manifest is not verified")
    _verify_manifest_payload(path.parent, manifest)
    return manifest


def cleanup_rebuildable_entries(
    run_root: Path,
    *,
    manifest_path: Path,
    entries: Iterable[str],
) -> dict[str, Any]:
    """Delete allowlisted rebuildable entries only after archive verification."""

    source = run_root.expanduser().resolve()
    manifest = verify_artifact_manifest(manifest_path)
    if manifest.get("source_run_root") != str(source):
        raise ArtifactArchiveError("artifact manifest does not belong to the requested run")
    requested = sorted(set(str(value) for value in entries))
    invalid = sorted(set(requested) - REBUILDABLE_ENTRY_ALLOWLIST)
    if invalid:
        raise ArtifactArchiveError(f"cleanup requested non-rebuildable entries: {invalid}")
    removed: list[str] = []
    bytes_removed = 0
    for name in requested:
        path = source / name
        if not path.exists() and not path.is_symlink():
            continue
        bytes_removed += _path_size(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(name)
    return {
        "status": "cleaned_after_verified_archive",
        "manifest_path": str(manifest_path.expanduser().resolve()),
        "removed_entries": removed,
        "bytes_removed": bytes_removed,
    }


def _run_auth_redactions(run_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in run_root.rglob("auth.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for value in _string_values(payload):
            if value:
                values[value] = "<redacted-model-source-key>"
    return values


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    return []


def _regular_files(root: Path) -> list[Path]:
    if root.is_file() and not root.is_symlink():
        return [root]
    if not root.is_dir() or root.is_symlink():
        return []
    return [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]


def _redact_archive_files(root: Path, redactions: dict[str, str]) -> dict[str, int]:
    encoded = [
        (secret.encode("utf-8"), replacement.encode("utf-8"))
        for secret, replacement in redactions.items()
        if secret
    ]
    counts: dict[str, int] = {}
    for path in _regular_files(root):
        data = path.read_bytes()
        changed = 0
        for secret, replacement in encoded:
            occurrences = data.count(secret)
            if occurrences:
                data = data.replace(secret, replacement)
                changed += occurrences
        if changed:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArtifactArchiveError(
                    f"credential value found in non-text artifact: {path.relative_to(root)}"
                ) from exc
            path.write_bytes(data)
            counts[str(path.relative_to(root))] = changed
    return counts


def _file_manifest(
    root: Path,
    source_hashes: dict[str, str],
    redaction_counts: dict[str, int],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in sorted(_regular_files(root)):
        relative = str(path.relative_to(root))
        results.append({
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "source_sha256": source_hashes.get(relative),
            "redaction_count": int(redaction_counts.get(relative) or 0),
        })
    return results


def _verify_manifest_payload(root: Path, manifest: dict[str, Any]) -> None:
    for item in manifest.get("files") or []:
        path = root / str(item.get("path") or "")
        if not path.is_file() or path.is_symlink():
            raise ArtifactArchiveError(f"archived artifact is missing: {item.get('path')}")
        expected_bytes = item.get("bytes")
        if not isinstance(expected_bytes, int) or path.stat().st_size != expected_bytes:
            raise ArtifactArchiveError(f"archived artifact size mismatch: {item.get('path')}")
        if _sha256_file(path) != item.get("sha256"):
            raise ArtifactArchiveError(f"archived artifact hash mismatch: {item.get('path')}")


def _write_catalog(path: Path, manifest: dict[str, Any]) -> None:
    lines = [
        f"# 真实验证 Artifacts：{manifest['run_id']}",
        "",
        f"- Phase：`{manifest['phase']}`",
        f"- Instance：`{manifest['instance_id']}`",
        f"- Source run：`{manifest['source_run_root']}`",
        f"- Status：`{manifest['status']}`",
        f"- Files：`{manifest['file_count']}`",
        f"- Bytes：`{manifest['total_bytes']}`",
        f"- Redaction：`{manifest['redaction_policy']}`",
        "",
        "## 已归档 Entries",
        "",
    ]
    lines.extend(f"- `{name}`" for name in manifest.get("selected_entries") or [])
    if manifest.get("omitted_files"):
        lines.extend(["", "## 省略文件", ""])
        lines.extend(
            f"- `{item['path']}`: {item['reason']}"
            for item in manifest["omitted_files"]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_size(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    if path.is_dir() and not path.is_symlink():
        return sum(item.stat().st_size for item in _regular_files(path))
    return 0


def _secure_tree(root: Path) -> None:
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
