"""
studio/retention.py — Safe retention reference engine and dry-run audit.

Part of Stage 7 Phase 4 (Operational Resilience), Milestone 2.
Identifies raw downloaded media in work/ eligible for future retention review,
verifies whether files are still referenced by active or persistent application
state (SQLite, jobs, queue, state.json), and reports findings.

STRICTLY READ-ONLY AND NON-DESTRUCTIVE:
- Never deletes, moves, renames, truncates, or modifies any media files.
- Real deletion pathways are NOT implemented.
- Uploaded output MP4s and poster PNGs are permanent and never deleted.
- Raw downloads must meet the minimum age of 7 days (604,800s) to be age-eligible.
- File age is derived strictly from the acquisition epoch in the filename
  (short_{epoch}_{key}_raw.mp4). Never falls back to ctime, mtime, or videos.created_at.
- Fails closed on any path ambiguity, symlink, database error, or unreadable state.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any

from studio import store

LOG = logging.getLogger("kenaushorts.retention")
ROOT = store.ROOT

# Retention policy constants
RETENTION_TARGET_DAYS: int = 7
RETENTION_TARGET_SECONDS: float = RETENTION_TARGET_DAYS * 86400.0  # 604,800 seconds

# Filename pattern for raw downloads: short_{epoch}_{key}_raw.mp4
RAW_FILENAME_RE = re.compile(
    r"^short_(?P<epoch>[0-9]{9,12})_(?P<key>[a-zA-Z0-9_-]+)_raw\.mp4$",
    re.IGNORECASE,
)

# Supported epoch bounds (sensible timestamp bounds: 2001 to 2049)
MIN_VALID_EPOCH: float = 1_000_000_000.0
MAX_VALID_EPOCH: float = 2_500_000_000.0
CLOCK_SKEW_TOLERANCE_SECONDS: float = 300.0  # 5 minutes

# Status constants
AGE_ELIGIBLE: str = "age_eligible"
TOO_YOUNG: str = "too_young"
UNKNOWN_AGE: str = "unknown_age"
INVALID_TIMESTAMP: str = "invalid_timestamp"

REFERENCED: str = "referenced"
UNREFERENCED: str = "unreferenced"
UNKNOWN_REFERENCE: str = "unknown_reference"

INSPECTION_ERROR: str = "inspection_error"
INVALID_PATH: str = "invalid_path"
INVALID_METADATA: str = "invalid_metadata"
AGE_ELIGIBLE_UNREFERENCED: str = "age_eligible_unreferenced"


def is_symlink_or_reparse_point(path: Path) -> bool:
    """
    Detect whether a path is a symbolic link, directory junction, or reparse point.
    Handles both POSIX symlinks and Windows reparse points safely without traversal.
    """
    try:
        if os.path.islink(path):
            return True
        if sys.platform == "win32":
            stat = os.lstat(path)
            # FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
            attrs = getattr(stat, "st_file_attributes", 0)
            if attrs & 0x0400:
                return True
    except (OSError, ValueError):
        # If lstat fails, fail closed by treating as suspicious
        return True
    return False


def validate_raw_path(path: Path, work_dir: Path) -> tuple[bool, str]:
    """
    Verify that candidate file resides strictly inside the approved work directory.
    Rejects directory traversal, alternate data streams (:), symlinks, and reparse points.
    """
    try:
        resolved_work = work_dir.resolve()
        resolved_path = path.resolve()
    except (OSError, ValueError) as e:
        return False, f"Failed to resolve path: {e}"

    # Containment check: must be a direct child of work_dir
    try:
        rel = resolved_path.relative_to(resolved_work)
    except ValueError:
        return False, f"Path '{path}' escapes approved root '{work_dir}'"

    if len(rel.parts) != 1:
        return False, f"Path '{path}' is not a direct child of '{work_dir}'"

    # Reject alternate data streams on Windows (e.g. file.mp4:stream)
    if ":" in path.name:
        return False, f"Alternate data stream detected in filename '{path.name}'"

    # Reject symlinks and junctions
    if is_symlink_or_reparse_point(path):
        return False, f"Symlink or reparse point rejected: '{path}'"

    return True, ""


def parse_raw_acquisition_timestamp(
    filename: str,
    current_time: float | None = None,
) -> tuple[str, float | None, str]:
    """
    Parse and validate the raw acquisition timestamp from the verified filename.
    Returns (status, epoch, diagnostic_message).
    Validates range, malformed syntax, and clock skew. Never uses ctime/mtime fallback.
    """
    match = RAW_FILENAME_RE.match(filename)
    if not match:
        return INVALID_METADATA, None, f"Filename '{filename}' does not match expected short_{{epoch}}_{{key}}_raw.mp4"

    raw_epoch_str = match.group("epoch")
    try:
        epoch = float(int(raw_epoch_str))
    except (ValueError, TypeError):
        return INVALID_TIMESTAMP, None, f"Malformed epoch value '{raw_epoch_str}' in '{filename}'"

    if not (MIN_VALID_EPOCH <= epoch <= MAX_VALID_EPOCH):
        return INVALID_TIMESTAMP, None, f"Epoch {epoch} outside supported range [{MIN_VALID_EPOCH}, {MAX_VALID_EPOCH}]"

    now_t = current_time if current_time is not None else time.time()
    if epoch > now_t + CLOCK_SKEW_TOLERANCE_SECONDS:
        return INVALID_TIMESTAMP, None, f"Timestamp is in the future: epoch {epoch} > current {now_t}"

    return "valid", epoch, ""


def evaluate_file_age(
    epoch: float | None,
    current_time: float | None = None,
) -> tuple[str, float, str]:
    """
    Evaluate file age strictly based on acquisition timestamp.
    Threshold is 7 days (604,800 seconds).
    Returns (age_status, age_seconds, diagnostic_message).
    """
    if epoch is None:
        return UNKNOWN_AGE, 0.0, "Missing or unparseable acquisition timestamp"

    now_t = current_time if current_time is not None else time.time()
    age_seconds = now_t - epoch

    if age_seconds < 0:
        return INVALID_TIMESTAMP, age_seconds, f"Negative file age ({age_seconds:.1f}s); future acquisition timestamp"

    age_days = round(age_seconds / 86400.0, 2)
    if age_seconds >= RETENTION_TARGET_SECONDS:
        return AGE_ELIGIBLE, age_seconds, f"Age ({age_days} days) meets 7-day retention target"
    else:
        return TOO_YOUNG, age_seconds, f"Age ({age_days} days) is under 7-day retention target"


def load_reference_index(root: Path | None = None) -> tuple[dict[str, Any] | None, str]:
    """
    Safely load and pre-index all relevant application state from SQLite and JSON.
    Reads SQLite 'videos' and 'jobs' tables and state.json under read-only locks.
    Fails closed if the database or state files are unreadable or corrupt.
    Returns (index_dict, error_message).
    """
    base_root = (root or ROOT).resolve()
    db_path = base_root / "studio.sqlite3"
    state_path = base_root / "state.json"

    index: dict[str, Any] = {
        "active_jobs": [],           # List of running/pending jobs
        "pending_retries": [],       # Jobs or videos in failed/render_failed state
        "unresolved_uploads": [],    # Videos in uploading/upload_unknown/upload_unresolved
        "ready_drafts": [],          # Videos in ready state
        "re_render_sources": {},     # orig_stem -> list of edit video IDs
        "video_records": {},         # normalized_raw_path -> list of video rec summaries
        "raw_paths_referenced": set(), # Set of normalized lowercase path strings
        "stems_referenced": set(),   # Set of lowercase stems (e.g. short_123_abc)
        "pipeline_busy": False,
    }

    # 1. Inspect pipeline lock (.pipeline.lock)
    try:
        lock_file = base_root / ".pipeline.lock"
        if lock_file.exists():
            if sys.platform == "win32":
                import msvcrt
                try:
                    with open(lock_file, "a") as h:
                        h.seek(0)
                        msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)
                        msvcrt.locking(h.fileno(), msvcrt.LK_UNLCK, 1)
                except (IOError, OSError):
                    index["pipeline_busy"] = True
            else:
                import fcntl
                try:
                    with open(lock_file, "a") as h:
                        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(h, fcntl.LOCK_UN)
                except (IOError, OSError):
                    index["pipeline_busy"] = True
    except Exception as e:
        LOG.warning("Could not probe pipeline lock (%s); proceeding with cautious indexing", e)

    # 2. Inspect SQLite tables (videos, jobs)
    if db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)) as db:
                db.row_factory = sqlite3.Row

                # A. Videos table
                cur = db.execute("SELECT id, data FROM videos")
                for row in cur.fetchall():
                    vid_id = str(row["id"])
                    try:
                        data = json.loads(row["data"])
                    except Exception as json_err:
                        return None, f"Corrupt video record '{vid_id}' in SQLite: {json_err}"

                    vid_status = data.get("status", "ready")
                    vid_stem = vid_id.lower()
                    index["stems_referenced"].add(vid_stem)

                    # Track re-render lineage (_edit_)
                    if "_edit_" in vid_id:
                        orig_key = vid_id.split("_edit_")[0].lower()
                        index["stems_referenced"].add(orig_key)
                        index["re_render_sources"].setdefault(orig_key, []).append(vid_id)

                    # Map explicit raw_video path if present
                    raw_v = data.get("raw_video")
                    if raw_v:
                        try:
                            rp = Path(raw_v)
                            if not rp.is_absolute():
                                rp = base_root / rp
                            norm_rp = os.path.normcase(str(rp.resolve()))
                            index["raw_paths_referenced"].add(norm_rp)
                            index["video_records"].setdefault(norm_rp, []).append({
                                "id": vid_id,
                                "status": vid_status,
                                "source": "raw_video_field",
                            })
                        except Exception:
                            pass

                    # Categorize state
                    if vid_status in ("uploading", "upload_unknown", "upload_unresolved"):
                        index["unresolved_uploads"].append(vid_id)
                    elif vid_status in ("failed", "render_failed"):
                        index["pending_retries"].append(vid_id)
                    elif vid_status == "ready":
                        index["ready_drafts"].append(vid_id)

                # B. Jobs table
                cur = db.execute("SELECT id, data FROM jobs")
                for row in cur.fetchall():
                    job_id = str(row["id"])
                    try:
                        job = json.loads(row["data"])
                    except Exception as json_err:
                        return None, f"Corrupt job record '{job_id}' in SQLite: {json_err}"

                    job_status = job.get("status", "pending")
                    job_key = (job.get("key") or "").lower()
                    if job_key:
                        index["stems_referenced"].add(job_key)
                        if "_edit_" in job_key:
                            orig_k = job_key.split("_edit_")[0].lower()
                            index["stems_referenced"].add(orig_k)

                    if job_status in ("pending", "running"):
                        index["active_jobs"].append({
                            "id": job_id,
                            "action": job.get("action"),
                            "key": job.get("key"),
                            "status": job_status,
                        })

                    # Also track target_file if set
                    target = job.get("target_file")
                    if target:
                        try:
                            tp = Path(target)
                            index["stems_referenced"].add(tp.stem.lower())
                        except Exception:
                            pass

        except sqlite3.Error as db_err:
            return None, f"Database access error on '{db_path}': {db_err}"
        except Exception as general_err:
            return None, f"Unexpected error reading SQLite: {general_err}"

    # 3. Inspect state.json
    if state_path.exists():
        try:
            raw_state = state_path.read_text(encoding="utf-8")
            sdata = json.loads(raw_state)
            if not isinstance(sdata, dict):
                return None, f"state.json must be a JSON object, got {type(sdata).__name__}"
            # Check posted and failed entries for referenced media
            for p in sdata.get("posted", []):
                v_path = p.get("video_path")
                if v_path:
                    try:
                        vp = Path(v_path)
                        index["stems_referenced"].add(vp.stem.lower())
                    except Exception:
                        pass
        except Exception as s_err:
            return None, f"state.json is corrupt or unreadable: {s_err}"

    return index, ""


def check_file_references(
    file_path: Path,
    ref_index: dict[str, Any] | None,
    index_error: str = "",
    root: Path | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    """
    Check whether candidate raw download file is referenced by application state.
    Uses normalized filesystem path comparisons and lineage matching.
    Fails closed with UNKNOWN_REFERENCE if reference index could not be verified.
    Returns (reference_status, reasons_list, diagnostic_summary).
    """
    if ref_index is None or index_error:
        return (
            UNKNOWN_REFERENCE,
            [],
            f"Reference state cannot be verified (fail closed): {index_error or 'Index unavailable'}",
        )

    reasons: list[dict[str, Any]] = []
    base_root = (root or ROOT).resolve()

    try:
        resolved_file = file_path.resolve()
        norm_file = os.path.normcase(str(resolved_file))
    except (OSError, ValueError) as resolve_err:
        return UNKNOWN_REFERENCE, [], f"Could not resolve file path: {resolve_err}"

    # Extract stem candidate key (e.g. short_{epoch}_{key}_raw -> short_{epoch}_{key})
    # Verified convention: short_{epoch}_{key}_raw.mp4 has stem short_{epoch}_{key}_raw
    raw_stem = file_path.stem
    video_stem = raw_stem[:-4].lower() if raw_stem.lower().endswith("_raw") else raw_stem.lower()

    # 1. Direct path reference in SQLite video records
    if norm_file in ref_index.get("raw_paths_referenced", set()):
        v_matches = ref_index.get("video_records", {}).get(norm_file, [])
        for m in v_matches:
            reasons.append({
                "source": "sqlite_videos",
                "field": "raw_video",
                "video_id": m.get("id"),
                "status": m.get("status"),
                "is_active_workflow": m.get("status") in (
                    "ready", "rendering", "uploading", "upload_unknown", "upload_unresolved", "failed", "render_failed"
                ),
            })

    # 2. Video ID / Stem match in SQLite
    if video_stem in ref_index.get("stems_referenced", set()):
        # Check re-render lineage
        edits = ref_index.get("re_render_sources", {}).get(video_stem, [])
        if edits:
            reasons.append({
                "source": "sqlite_videos_re_render",
                "field": "lineage_re_render_source",
                "stem": video_stem,
                "edits": edits,
                "is_active_workflow": True,
            })
        else:
            reasons.append({
                "source": "sqlite_videos_id",
                "field": "video_id_stem",
                "stem": video_stem,
                "is_active_workflow": True,
            })

    # 3. Active / Pending jobs match
    for job in ref_index.get("active_jobs", []):
        j_key = (job.get("key") or "").lower()
        if j_key == video_stem or (j_key and video_stem.startswith(j_key)):
            reasons.append({
                "source": "sqlite_jobs_active",
                "job_id": job.get("id"),
                "action": job.get("action"),
                "status": job.get("status"),
                "is_active_workflow": True,
            })

    # 4. Pipeline lock active
    if ref_index.get("pipeline_busy"):
        # If pipeline is busy and file was recently modified (within 2 hours), assume in-flight
        try:
            mtime = resolved_file.stat().st_mtime
            if time.time() - mtime < 7200:
                reasons.append({
                    "source": "pipeline_lock",
                    "reason": "Pipeline is actively executing video workflow on host",
                    "is_active_workflow": True,
                })
        except Exception:
            pass

    if reasons:
        return REFERENCED, reasons, f"Referenced by {len(reasons)} state source(s)"
    else:
        return UNREFERENCED, [], "No references found in successfully inspected state"


def classify_retention(
    path_valid: bool,
    path_error: str,
    age_status: str,
    ref_status: str,
    inspection_error: str = "",
) -> tuple[str, str]:
    """
    Combine age and reference assessments into a conservative retention status.
    Strict safety precedence:
    1. inspection_error
    2. invalid_path / invalid_metadata
    3. unknown_reference
    4. unknown_age / invalid_timestamp
    5. referenced
    6. too_young
    7. age_eligible_unreferenced (only when 100% verified unreferenced and >= 7 days)
    """
    if inspection_error:
        return INSPECTION_ERROR, f"Inspection error: {inspection_error}"

    if not path_valid:
        return INVALID_PATH, f"Invalid path or containment: {path_error}"

    if ref_status == UNKNOWN_REFERENCE:
        return UNKNOWN_REFERENCE, "Reference status uncertain or state unreadable (fail closed)"

    if age_status in (UNKNOWN_AGE, INVALID_TIMESTAMP):
        return age_status, "Acquisition timestamp invalid, missing, or in the future"

    if ref_status == REFERENCED:
        return REFERENCED, "File is referenced by application state or active workflow"

    if age_status == TOO_YOUNG:
        return TOO_YOUNG, "File acquisition age is under the 7-day retention threshold"

    if age_status == AGE_ELIGIBLE and ref_status == UNREFERENCED:
        return AGE_ELIGIBLE_UNREFERENCED, "Retention review candidate: older than 7 days and confirmed unreferenced"

    # Default conservative fallback
    return UNKNOWN_REFERENCE, "Unclassified state; failing closed"


def audit_retention(
    work_dir: Path | None = None,
    current_time: float | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """
    Conduct a dry-run retention audit of candidate raw downloads in work/.
    Strictly read-only and non-destructive. Returns structured results with summary.
    """
    base_root = (root or ROOT).resolve()
    target_work = (work_dir or (base_root / "work")).resolve()
    now_t = current_time if current_time is not None else time.time()

    summary: dict[str, Any] = {
        "total_files_scanned": 0,
        "age_eligible_unreferenced_count": 0,
        "too_young_count": 0,
        "referenced_count": 0,
        "unknown_age_count": 0,
        "unknown_reference_count": 0,
        "invalid_path_count": 0,
        "inspection_error_count": 0,
        "estimated_reclaimable_bytes": 0,
        "estimated_reclaimable_mb": 0.0,
    }

    results: list[dict[str, Any]] = []

    if not target_work.exists() or not target_work.is_dir():
        return {
            "mode": "dry_run",
            "deletion_enabled": False,
            "deletion_occurred": False,
            "work_dir": str(target_work.name),
            "summary": summary,
            "files": [],
            "message": "Work directory does not exist or is not a directory",
        }

    # Load reference index (fails closed if database / state is corrupt or unavailable)
    ref_index, index_error = load_reference_index(root=base_root)

    # Discover candidate files in work/
    try:
        entries = sorted(target_work.iterdir(), key=lambda p: p.name)
    except Exception as dir_err:
        LOG.error("Failed to list work directory '%s': %s", target_work, dir_err)
        return {
            "mode": "dry_run",
            "deletion_enabled": False,
            "deletion_occurred": False,
            "work_dir": str(target_work.name),
            "summary": summary,
            "files": [],
            "error": f"Failed to list work directory: {dir_err}",
        }

    for entry in entries:
        # Focus strictly on raw media download files (*_raw.mp4)
        if not entry.name.lower().endswith("_raw.mp4"):
            continue

        summary["total_files_scanned"] += 1
        entry_diag: dict[str, Any] = {
            "name": entry.name,
            "relative_path": f"{target_work.name}/{entry.name}",
            "size_bytes": 0,
            "size_mb": 0.0,
            "acquisition_epoch": None,
            "age_days": None,
            "age_status": UNKNOWN_AGE,
            "reference_status": UNKNOWN_REFERENCE,
            "retention_status": UNKNOWN_REFERENCE,
            "reasons": [],
            "message": "",
        }

        # 1. Path safety and containment check
        path_valid, path_err = validate_raw_path(entry, target_work)
        if not path_valid:
            entry_diag["retention_status"] = INVALID_PATH
            entry_diag["message"] = path_err
            summary["invalid_path_count"] += 1
            results.append(entry_diag)
            continue

        # 2. File stat (size check without loading into memory)
        try:
            stat_res = entry.stat()
            file_size = stat_res.st_size
            entry_diag["size_bytes"] = file_size
            entry_diag["size_mb"] = round(file_size / (1024 * 1024), 2)
        except Exception as stat_err:
            entry_diag["retention_status"] = INSPECTION_ERROR
            entry_diag["message"] = f"Stat error: {stat_err}"
            summary["inspection_error_count"] += 1
            results.append(entry_diag)
            continue

        # 3. File age assessment from acquisition epoch
        ts_status, epoch_val, ts_err = parse_raw_acquisition_timestamp(entry.name, current_time=now_t)
        if ts_status != "valid" or epoch_val is None:
            entry_diag["age_status"] = ts_status
            entry_diag["message"] = ts_err
            age_status = ts_status
            age_seconds = 0.0
        else:
            entry_diag["acquisition_epoch"] = epoch_val
            age_status, age_seconds, age_msg = evaluate_file_age(epoch_val, current_time=now_t)
            entry_diag["age_status"] = age_status
            entry_diag["age_days"] = round(age_seconds / 86400.0, 2)
            entry_diag["message"] = age_msg

        # 4. Reference detection across application state
        ref_status, ref_reasons, ref_msg = check_file_references(
            entry, ref_index, index_error=index_error, root=base_root
        )
        entry_diag["reference_status"] = ref_status
        entry_diag["reasons"] = ref_reasons

        # 5. Combined conservative retention classification
        ret_status, ret_msg = classify_retention(
            path_valid=True,
            path_error="",
            age_status=age_status,
            ref_status=ref_status,
            inspection_error="",
        )
        entry_diag["retention_status"] = ret_status
        if not entry_diag["message"] or ret_status in (REFERENCED, AGE_ELIGIBLE_UNREFERENCED):
            entry_diag["message"] = ret_msg

        # Update summary counters
        if ret_status == AGE_ELIGIBLE_UNREFERENCED:
            summary["age_eligible_unreferenced_count"] += 1
            summary["estimated_reclaimable_bytes"] += entry_diag["size_bytes"]
        elif ret_status == TOO_YOUNG:
            summary["too_young_count"] += 1
        elif ret_status == REFERENCED:
            summary["referenced_count"] += 1
        elif ret_status in (UNKNOWN_AGE, INVALID_TIMESTAMP):
            summary["unknown_age_count"] += 1
        elif ret_status == UNKNOWN_REFERENCE:
            summary["unknown_reference_count"] += 1
        elif ret_status in (INVALID_PATH, INVALID_METADATA):
            summary["invalid_path_count"] += 1
        elif ret_status == INSPECTION_ERROR:
            summary["inspection_error_count"] += 1

        results.append(entry_diag)

    summary["estimated_reclaimable_mb"] = round(summary["estimated_reclaimable_bytes"] / (1024 * 1024), 2)

    return {
        "mode": "dry_run",
        "deletion_enabled": False,
        "deletion_occurred": False,
        "work_dir": str(target_work.name),
        "audit_timestamp": now_t,
        "retention_target_days": RETENTION_TARGET_DAYS,
        "summary": summary,
        "files": results,
    }
