use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::json;
use uuid::Uuid;

fn evidence_dir() -> Option<PathBuf> {
    let raw = std::env::var("BENCH_EVIDENCE_DIR").ok()?;
    if raw.trim().is_empty() {
        return None;
    }
    Some(PathBuf::from(raw))
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn write_json(path: PathBuf, payload: serde_json::Value) {
    if let Some(parent) = path.parent()
        && let Err(err) = std::fs::create_dir_all(parent)
    {
        tracing::warn!(
            "Failed to create benchmark evidence dir {}: {}",
            parent.display(),
            err
        );
        return;
    }

    match serde_json::to_vec(&payload) {
        Ok(bytes) => {
            if let Err(err) = std::fs::write(&path, bytes) {
                tracing::warn!(
                    "Failed to write benchmark evidence file {}: {}",
                    path.display(),
                    err
                );
            }
        }
        Err(err) => {
            tracing::warn!(
                "Failed to serialize benchmark evidence for {}: {}",
                path.display(),
                err
            );
        }
    }
}

pub fn write_job_created(job_id: Uuid, project_dir: Option<&std::path::Path>, mode: &str) {
    let Some(dir) = evidence_dir() else {
        return;
    };

    write_json(
        dir.join(format!("job-created-{}.json", job_id)),
        json!({
            "event": "job_created",
            "job_id": job_id.to_string(),
            "mode": mode,
            "project_dir": project_dir.map(|p| p.display().to_string()),
            "ts_unix_ms": now_unix_ms(),
        }),
    );
}

pub fn write_worker_callback(job_id: Uuid, success: bool, message: Option<&str>) {
    let Some(dir) = evidence_dir() else {
        return;
    };

    write_json(
        dir.join(format!("worker-callback-{}.json", job_id)),
        json!({
            "event": "worker_callback",
            "job_id": job_id.to_string(),
            "success": success,
            "message": message,
            "ts_unix_ms": now_unix_ms(),
        }),
    );
}

pub fn write_worker_cleaned(job_id: Uuid, container_id: Option<&str>, container_removed: bool) {
    let Some(dir) = evidence_dir() else {
        return;
    };

    write_json(
        dir.join(format!("worker-cleaned-{}.json", job_id)),
        json!({
            "event": "worker_cleaned",
            "job_id": job_id.to_string(),
            "container_id": container_id,
            "container_removed": container_removed,
            "ts_unix_ms": now_unix_ms(),
        }),
    );
}

pub fn write_agent_workspace_written(
    db_path: Option<&std::path::Path>,
    imported_files: usize,
    seeded_files: usize,
    backend: &str,
) {
    let Some(dir) = evidence_dir() else {
        return;
    };

    let size_bytes = db_path
        .and_then(|path| std::fs::metadata(path).ok())
        .map(|meta| meta.len());

    write_json(
        dir.join("agent-workspace-written.json"),
        json!({
            "event": "agent_workspace_written",
            "backend": backend,
            "path": db_path.map(|p| p.display().to_string()),
            "imported_files": imported_files,
            "seeded_files": seeded_files,
            "files_written": imported_files + seeded_files,
            "size_bytes": size_bytes,
            "ts_unix_ms": now_unix_ms(),
        }),
    );
}
