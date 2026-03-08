use std::path::PathBuf;
use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;
use chrono::Utc;
use serde::Deserialize;
use uuid::Uuid;

use crate::bootstrap::ironclaw_base_dir;
use crate::channels::web::server::GatewayState;
use crate::history::SandboxJobRecord;
use crate::orchestrator::job_manager::JobMode;

#[derive(Debug, Deserialize)]
pub struct CreateExternalWorkerRequest {
    pub task: String,
}

#[derive(Debug, serde::Serialize)]
pub struct CreateExternalWorkerResponse {
    pub job_id: String,
    pub worker_token: String,
    pub project_dir: String,
    pub orchestrator_url: String,
}

fn create_project_dir(job_id: Uuid) -> Result<PathBuf, (StatusCode, String)> {
    let base = ironclaw_base_dir().join("projects");
    std::fs::create_dir_all(&base)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let project_dir = base.join(job_id.to_string());
    std::fs::create_dir_all(&project_dir)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let mut perms = std::fs::metadata(&project_dir)
            .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?
            .permissions();
        perms.set_mode(0o777);
        std::fs::set_permissions(&project_dir, perms)
            .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    }

    project_dir
        .canonicalize()
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))
}

fn persist_record(
    store: Arc<dyn crate::db::Database>,
    user_id: String,
    job_id: Uuid,
    task: String,
    project_dir: String,
) {
    tokio::spawn(async move {
        let now = Utc::now();
        let record = SandboxJobRecord {
            id: job_id,
            task,
            status: "creating".to_string(),
            user_id,
            project_dir: project_dir.clone(),
            success: None,
            failure_reason: None,
            created_at: now,
            started_at: None,
            completed_at: None,
            credential_grants_json: "[]".to_string(),
        };
        if let Err(err) = store.save_sandbox_job(&record).await {
            tracing::warn!(job_id = %job_id, "Failed to persist external benchmark job: {}", err);
            return;
        }
        if let Err(err) = store
            .update_sandbox_job_status(job_id, "running", None, None, Some(now), None)
            .await
        {
            tracing::warn!(job_id = %job_id, "Failed to update external benchmark job status: {}", err);
        }
    });
}

pub async fn benchmark_create_external_worker_handler(
    State(state): State<Arc<GatewayState>>,
    Json(req): Json<CreateExternalWorkerRequest>,
) -> Result<Json<CreateExternalWorkerResponse>, (StatusCode, String)> {
    let job_manager = state.job_manager.clone().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "Job manager not available".to_string(),
    ))?;

    let task = req.task.trim();
    if task.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "task must not be empty".to_string()));
    }

    let job_id = Uuid::new_v4();
    let project_dir = create_project_dir(job_id)?;
    let project_dir_str = project_dir.display().to_string();

    let worker_token = job_manager
        .create_external_job(job_id, task, Some(project_dir.clone()), JobMode::Worker, vec![])
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    crate::benchmark_evidence::write_job_created(job_id, Some(&project_dir), "worker");

    if let Some(store) = state.store.clone() {
        persist_record(
            store,
            state.user_id.clone(),
            job_id,
            task.to_string(),
            project_dir_str.clone(),
        );
    }

    Ok(Json(CreateExternalWorkerResponse {
        job_id: job_id.to_string(),
        worker_token,
        project_dir: project_dir_str,
        orchestrator_url: "http://127.0.0.1:50051".to_string(),
    }))
}
