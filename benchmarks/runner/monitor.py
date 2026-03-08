#!/usr/bin/env python3
"""
Live topology monitor for synthetic isolation benchmarks.

Serves a small local webpage and tails per-agent JSONL event logs so the user
can watch agent and worker lifecycles while a benchmark is running.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Benchmark Monitor</title>
<style>
:root {
  --bg: #0f1117;
  --surface: #1a1d27;
  --border: #2a2d3a;
  --text: #e1e4ed;
  --text-dim: #8b8fa3;
  --accent: #6c8cff;
  --green: #4ade80;
  --red: #f87171;
  --orange: #fbbf24;
  --agent-bg: #1e2233;
  --agent-border: #3b4261;
  --worker-bg: #1e2a1e;
  --worker-border: #2e4a2e;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  padding: 8px 12px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  margin-bottom: 8px;
  padding: 6px 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.header-left { display: flex; align-items: center; gap: 10px; z-index: 1; min-width: 0; }
.header-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 13px; font-weight: 600; white-space: nowrap; }
.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  white-space: nowrap;
}
.badge-approach { background: #2a3552; color: var(--accent); }
.badge-mode { background: #2a3a2a; color: var(--green); }
.badge-phase { background: #3a2a2a; color: var(--orange); }
.badge-phase.running { background: #1a3a1a; color: var(--green); }
.badge-phase.done { background: #2a2a3a; color: var(--text-dim); }
.progress-text {
  font-size: 10px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stat { display: flex; gap: 4px; font-size: 11px; white-space: nowrap; }
.stat-label { color: var(--text-dim); }
.stat-value { color: var(--text); font-weight: 600; }
.summary-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(116px, 1fr));
  gap: 4px;
  margin-bottom: 8px;
}
.summary-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 5px 6px;
  min-width: 0;
}
.summary-label {
  display: block;
  font-size: 9px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.summary-value {
  display: block;
  font-size: 12px;
  color: var(--text);
  font-weight: 700;
  white-space: nowrap;
}
.compact-grid {
  display: grid;
  gap: 4px;
  grid-template-columns: repeat(var(--cols, 10), 1fr);
}
.compact-tile {
  background: var(--agent-bg);
  border: 1.5px solid var(--agent-border);
  border-radius: 4px;
  padding: 4px 6px;
  min-width: 0;
  transition: border-color 0.3s, opacity 0.3s;
  cursor: pointer;
  position: relative;
  overflow: visible;
}
@keyframes float-up {
  0%   { opacity: 1; transform: translateY(0); }
  100% { opacity: 0; transform: translateY(-28px); }
}
.float-event {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translateX(-50%);
  font-size: 9px;
  font-weight: 600;
  color: var(--green);
  white-space: nowrap;
  pointer-events: none;
  animation: float-up 1.6s ease-out forwards;
}
.float-event.storage { color: var(--accent); }
.compact-tile:hover {
  border-color: var(--accent);
  opacity: 1 !important;
}
.compact-tile.pending { opacity: 0.72; }
.compact-tile.stopped { opacity: 0.66; }
.compact-tile.unhealthy { border-color: var(--red); opacity: 0.6; }
.compact-tile-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 3px;
  gap: 6px;
}
.compact-tile-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 9px;
  font-weight: 600;
  color: var(--text);
}
.compact-tile-count {
  font-size: 9px;
  color: var(--text-dim);
  white-space: nowrap;
}
.compact-tile-dots {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
}
/* Medium tiles (3-5 columns) */
.compact-grid.size-md .compact-tile { padding: 8px 10px; }
.compact-grid.size-md .compact-tile-header { margin-bottom: 5px; }
.compact-grid.size-md .compact-tile-name { font-size: 12px; }
.compact-grid.size-md .compact-tile-count { font-size: 11px; }
.compact-grid.size-md .compact-tile-dots { gap: 4px; }
.compact-grid.size-md .compact-dot { width: 22px; height: 12px; border-radius: 3px; }
.compact-grid.size-md .compact-dot.emoji-dot { font-size: 15px; line-height: 12px; }
.compact-grid.size-md .compact-dot.timer-dot { font-size: 9px; line-height: 12px; }
/* Large tiles (1-2 columns) */
.compact-grid.size-lg .compact-tile { padding: 12px 16px; }
.compact-grid.size-lg .compact-tile-header { margin-bottom: 8px; }
.compact-grid.size-lg .compact-tile-name { font-size: 15px; }
.compact-grid.size-lg .compact-tile-count { font-size: 13px; }
.compact-grid.size-lg .compact-tile-dots { gap: 5px; }
.compact-grid.size-lg .compact-dot { width: 30px; height: 18px; border-radius: 3px; }
.compact-grid.size-lg .compact-dot.emoji-dot { font-size: 22px; line-height: 18px; }
.compact-grid.size-lg .compact-dot.timer-dot { font-size: 12px; line-height: 18px; }
.compact-dot {
  width: 14px;
  height: 8px;
  border-radius: 2px;
  background: var(--border);
  transition: background 0.3s;
}
.compact-dot.filled {
  background: var(--green);
}
.compact-dot.emoji-dot {
  background: none;
  font-size: 10px;
  line-height: 8px;
  text-align: center;
  overflow: hidden;
}
.compact-dot.timer-dot {
  position: relative;
  font-size: 7px;
  line-height: 8px;
  text-align: center;
  color: rgba(0,0,0,0.5);
  font-variant-numeric: tabular-nums;
  overflow: hidden;
}
.empty-msg {
  font-size: 11px;
  color: var(--text-dim);
  font-style: italic;
  padding: 4px 0;
}
.lifecycle-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 8px;
  padding: 4px 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 9px;
  color: var(--text-dim);
  overflow-x: auto;
  white-space: nowrap;
}
.lifecycle-bar:empty { display: none; }
.lifecycle-step {
  padding: 2px 6px;
  border-radius: 3px;
  background: var(--border);
  color: var(--text);
  font-weight: 500;
}
.lifecycle-arrow { color: var(--text-dim); }
@media (max-width: 600px) {
  .header { flex-direction: column; align-items: flex-start; }
}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>Agent Monitor</h1>
    <span class="badge badge-approach" id="badge-approach">-</span>
    <span class="badge badge-mode" id="badge-mode">-</span>
    <span class="badge badge-phase" id="badge-phase">setup</span>
    <span class="progress-text" id="progress-text"></span>
  </div>
  <div class="header-right">
    <span class="stat"><span class="stat-label">Agents</span> <span class="stat-value" id="hdr-agents">-</span></span>
    <span class="stat"><span class="stat-label">Workers</span> <span class="stat-value" id="hdr-workers">-</span></span>
  </div>
</div>

<div class="summary-strip" id="summary-strip"></div>
<div class="lifecycle-bar" id="lifecycle-bar"></div>
<div id="main-area"></div>

<script>
const STATE_URL = "/api/state";
const POLL_MS = 800;

// Stable slot assignments: agentId -> { workerId -> slotIndex }
const slotMap = {};
// Previous worker state snapshot for detecting transitions.
const prevWorkerState = {};

function emitFloatEvent(tileEl, text, cls) {
  const el = document.createElement("span");
  el.className = "float-event" + (cls ? " " + cls : "");
  el.textContent = text;
  tileEl.appendChild(el);
  el.addEventListener("animationend", () => el.remove());
}

function formatTime(seconds) {
  const s = Math.max(0, seconds || 0);
  if (s < 10) return `${s.toFixed(1)}s`;
  const whole = Math.floor(s);
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  const remaining = whole % 60;
  return `${minutes}m${String(remaining).padStart(2, "0")}s`;
}

function gridColumns(agentCount) {
  const n = agentCount || 1;
  const width = document.getElementById("main-area").clientWidth || window.innerWidth;
  const maxCols = Math.floor((width + 4) / 104);
  // Prefer fewer columns for wider tiles: round(sqrt(n)), capped by screen width.
  const sqrtCols = Math.max(1, Math.round(Math.sqrt(n)));
  return Math.max(1, Math.min(sqrtCols, maxCols));
}

function statusClass(agent) {
  if (agent.status === "stopped") return "stopped";
  if (agent.status === "pending") return "pending";
  return "";
}

function healthClass(agent) {
  return agent.status === "running" || agent.status === "stopped" ? "" : "unhealthy";
}

function ratio(value, total) {
  return `${value}/${total}`;
}

function renderSummary(state) {
  const lifecycle = state.lifecycle || {};
  const expectedAgents = state.expected_agents || state.num_agents || 0;
  const launchedWorkers = lifecycle.workers_launched || 0;
  const inactiveWorkers = launchedWorkers - (state.active_workers || 0);
  document.getElementById("hdr-agents").textContent = ratio(state.started_agents || 0, expectedAgents);
  document.getElementById("hdr-workers").textContent = String(state.active_workers || 0);
  const stats = [
    ["Cumulative Workers", String(launchedWorkers)],
    ["Worker Callbacks", String(lifecycle.successful_checkins || 0)],
    ["Persisted Worker Writes", String(lifecycle.worker_storage_written || 0)],
    ["Clean Exits", ratio(lifecycle.workers_finished || 0, inactiveWorkers)],
  ];
  document.getElementById("summary-strip").innerHTML = stats.map(([label, value]) => `
    <div class="summary-card">
      <span class="summary-label">${label}</span>
      <span class="summary-value">${value}</span>
    </div>
  `).join("");
}

function renderCompact(state) {
  const area = document.getElementById("main-area");
  const agents = state.agents || [];
  const maxSlots = Math.max(1, state.max_worker_slots || 1);

  if (agents.length === 0) {
    area.innerHTML = '<span class="empty-msg">waiting for agents...</span>';
    return;
  }

  const cols = gridColumns(state.num_agents);
  const cards = agents.map(agent => {
    const now = Date.now() / 1000;
    const allWorkers = agent.workers || [];

    // Maintain stable slot assignments for this agent.
    if (!slotMap[agent.id]) slotMap[agent.id] = {};
    const slots = slotMap[agent.id];
    const currentIds = new Set(allWorkers.map(w => w.id));

    // Free slots for workers no longer present.
    for (const [wid, _] of Object.entries(slots)) {
      if (!currentIds.has(wid)) delete slots[wid];
    }

    // Assign slots to new workers (lowest available index).
    const usedSlots = new Set(Object.values(slots));
    for (const w of allWorkers) {
      if (slots[w.id] == null) {
        for (let s = 0; s < maxSlots; s++) {
          if (!usedSlots.has(s)) { slots[w.id] = s; usedSlots.add(s); break; }
        }
      }
    }

    // Build a slot array: each index holds the worker (or null).
    const slotArr = new Array(maxSlots).fill(null);
    for (const w of allWorkers) {
      const s = slots[w.id];
      if (s != null && s < maxSlots) slotArr[s] = w;
    }

    const dots = slotArr.map((w, i) => {
      if (!w) return `<div class="compact-dot"></div>`;
      if (w.checkin_emoji) return `<div class="compact-dot filled emoji-dot" title="${w.id} callback">${w.checkin_emoji}</div>`;
      if (w.started_at) {
        const elapsed = Math.round(now - w.started_at);
        return `<div class="compact-dot filled timer-dot" title="${w.id} waiting for checkin">${elapsed}s</div>`;
      }
      return `<div class="compact-dot filled"></div>`;
    });
    const hasGw = !!agent.gateway_port;
    const click = hasGw ? `onclick="window.open('/agent-loader/${agent.id}/', '_blank')"` : "";
    const tip = hasGw ? `${agent.id} — click to open UI` : agent.id;
    return `
      <div class="compact-tile ${statusClass(agent)} ${healthClass(agent)}" title="${tip}" ${click}>
        <div class="compact-tile-header">
          <span class="compact-tile-name">${agent.id}${hasGw ? " ↗" : ""}</span>
          <span class="compact-tile-count">${agent.active_workers}/${maxSlots}</span>
        </div>
        <div class="compact-tile-dots">${dots.join("")}</div>
      </div>
    `;
  }).join("");

  const sizeClass = cols <= 2 ? 'size-lg' : cols <= 5 ? 'size-md' : '';
  area.innerHTML = `<div class="compact-grid ${sizeClass}" style="--cols:${cols}">${cards}</div>`;

  // Detect worker state transitions and emit floating event labels.
  const tiles = area.querySelectorAll(".compact-tile");
  agents.forEach((agent, ai) => {
    const tile = tiles[ai];
    if (!tile) return;
    const workers = agent.workers || [];
    if (!prevWorkerState[agent.id]) prevWorkerState[agent.id] = {};
    const prev = prevWorkerState[agent.id];
    for (const w of workers) {
      const p = prev[w.id] || {};
      if (w.checkin_emoji && !p.checkin_emoji) {
        emitFloatEvent(tile, "callback " + w.checkin_emoji, "");
      } else if (w.storage_written && !p.storage_written) {
        emitFloatEvent(tile, "storage write", "storage");
      }
      prev[w.id] = { checkin_emoji: w.checkin_emoji, storage_written: w.storage_written };
    }
    // Clean up old entries.
    const currentIds = new Set(workers.map(w => w.id));
    for (const wid of Object.keys(prev)) {
      if (!currentIds.has(wid)) delete prev[wid];
    }
  });
}

function phaseClass(phase) {
  if (phase === "running") return "running";
  if (phase === "finished") return "done";
  return "";
}

function progressText(state) {
  if (state.phase === "running") {
    return `${formatTime(state.elapsed_s)} / ${formatTime(state.duration_s)}`;
  }
  if (state.phase === "finished") {
    return "complete";
  }
  if (state.phase_detail) {
    return state.phase_detail;
  }
  return "";
}

function update(state) {
  document.title = `${state.approach || "benchmark"} monitor`;
  document.getElementById("badge-approach").textContent = state.approach || "-";
  document.getElementById("badge-mode").textContent = state.mode || "-";

  const phaseBadge = document.getElementById("badge-phase");
  phaseBadge.textContent = state.phase || "-";
  phaseBadge.className = `badge badge-phase ${phaseClass(state.phase || "")}`;

  document.getElementById("progress-text").textContent = progressText(state);

  renderSummary(state);
  renderLifecycle(state);
  renderCompact(state);
}

function renderLifecycle(state) {
  const bar = document.getElementById("lifecycle-bar");
  const p = state.job_params || {};
  if (!p.profile) { bar.innerHTML = ""; return; }
  const steps = ["start"];
  if (p.checkin) steps.push("sleep 3s", "worker callback (random emoji)", "sleep 3s");
  steps.push("storage write");
  const dur = p.duration_min_s === p.duration_max_s
    ? `sleep ${p.duration_min_s}s`
    : `sleep ${p.duration_min_s}-${p.duration_max_s}s`;
  if (p.profile === "memory-touch") {
    steps.push(`alloc ${p.memory_mb}MB + ${dur}`);
  } else if (p.duration_min_s > 0) {
    steps.push(dur);
  }
  steps.push("exit");
  bar.innerHTML = '<span style="margin-right:2px">Worker lifecycle:</span>' +
    steps.map(s => `<span class="lifecycle-step">${s}</span>`).join('<span class="lifecycle-arrow">\u2192</span>');
}

async function tick() {
  try {
    const response = await fetch(STATE_URL, { cache: "no-store" });
    if (response.ok) update(await response.json());
  } finally {
    window.setTimeout(tick, POLL_MS);
  }
}

tick();
window.addEventListener("resize", () => {
  fetch(STATE_URL, { cache: "no-store" })
    .then(response => response.json())
    .then(update)
    .catch(() => {});
});
</script>
</body>
</html>
"""

_AGENT_PROXY_RE = re.compile(r"^/agent/(agent-\d+)(/.*)?$")

# Service worker that intercepts all fetches within /agent/<id>/ scope
# and rewrites absolute paths to include the proxy prefix.
_SW_JS_TEMPLATE = """
var BASE = "{base}";
self.addEventListener("fetch", function(e) {{
  var url = new URL(e.request.url);
  if (url.origin === self.location.origin && !url.pathname.startsWith(BASE)) {{
    var newUrl = new URL(BASE + url.pathname + url.search, url.origin);
    e.respondWith(fetch(new Request(newUrl, e.request)));
  }}
}});
"""

# Loader page: registers the service worker then navigates to the real page
_AGENT_LOADER_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{agent_id}</title>
<style>body{{background:#0f1117;color:#e1e4ed;font-family:monospace;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}
</style></head><body><div>Loading {agent_id}...</div><script>
if ("serviceWorker" in navigator) {{
  navigator.serviceWorker.register("{base}/sw.js", {{scope: "{base}/"}})
    .then(function(reg) {{
      if (reg.active) {{ window.location.replace("{base}/?token=bench-token"); }}
      else {{
        var w = reg.installing || reg.waiting;
        w.addEventListener("statechange", function() {{
          if (w.state === "activated") window.location.replace("{base}/?token=bench-token");
        }});
      }}
    }});
}} else {{ window.location.replace("{base}/?token=bench-token"); }}
</script></body></html>
"""



def _agent_index(agent_id: str) -> int:
    try:
        return int(str(agent_id).rsplit("-", 1)[-1])
    except (IndexError, ValueError):
        return 0


def _worker_index(worker_id: str) -> int:
    try:
        return int(str(worker_id).rsplit("-", 1)[-1])
    except (IndexError, ValueError):
        return 0


def _blank_worker(worker_id: str) -> dict:
    return {
        "id": worker_id,
        "index": _worker_index(worker_id),
        "status": "running",
        "started_at": None,
        "checked_in": False,
        "checkin_at": None,
        "checkin_emoji": None,
        "cold_start_ms": None,
        "storage_written": False,
        "exited": False,
        "rss_kb": -1,
    }


def _blank_agent(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "index": _agent_index(agent_id),
        "status": "pending",
        "benchmark_started": False,
        "active_workers": 0,
        "total_spawned": 0,
        "total_checkins": 0,
        "total_completed": 0,
        "total_worker_storage_written": 0,
        "total_agent_storage_verified": 0,
        "worker_backend": None,
        "worker_runtime": None,
        "last_event_at": None,
        "workers": {},
        "_spawned_ids": set(),
        "_checkin_ids": set(),
        "_completed_ids": set(),
        "_worker_storage_ids": set(),
        "_agent_storage_ids": set(),
    }


class MonitorState:
    """Thread-safe benchmark monitor state."""

    def __init__(
        self,
        approach: str,
        mode: str,
        run_id: str,
        expected_agents: List[str],
        duration_s: int,
        max_worker_slots: int,
    ) -> None:
        now = time.time()
        self._lock = threading.Lock()
        self._state = {
            "approach": approach,
            "mode": mode,
            "run_id": run_id,
            "duration_s": duration_s,
            "max_worker_slots": max_worker_slots,
            "phase": "initializing",
            "phase_detail": "",
            "created_at": now,
            "updated_at": now,
            "last_event_at": None,
            "running_started_at": None,
            "expected_agents": len(expected_agents),
            "agent_order": list(expected_agents),
            "agents": {agent_id: _blank_agent(agent_id) for agent_id in expected_agents},
            "job_params": {},
        }

    def set_job_params(self, params: dict) -> None:
        with self._lock:
            self._state["job_params"] = dict(params)

    def set_phase(self, phase: str, detail: str = "") -> None:
        with self._lock:
            self._state["phase"] = phase
            self._state["phase_detail"] = detail
            if phase == "running" and self._state["running_started_at"] is None:
                self._state["running_started_at"] = time.time()
            self._state["updated_at"] = time.time()

    def set_agent_gateways(self, gateways: Dict[str, int]) -> None:
        with self._lock:
            self._state.setdefault("agent_gateways", {}).update(gateways)
            self._state["updated_at"] = time.time()

    def get_agent_gateways(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._state.get("agent_gateways", {}))

    def attach_agents(self, agent_ids: List[str]) -> None:
        with self._lock:
            self._state["agent_order"] = list(agent_ids)
            self._state["expected_agents"] = len(agent_ids)
            for agent_id in agent_ids:
                self._ensure_agent(agent_id)
            self._state["updated_at"] = time.time()

    _EVENT_ALIASES = {
        "agent_started": "agent_start",
        "agent_exited": "agent_stop",
        "agent_exiting": "agent_stop",
    }

    def _maybe_count_clean(self, agent: dict, worker_id: str, worker: dict) -> None:
        """Count a worker as a clean exit if all conditions are met."""
        if not worker["exited"]:
            return
        if not worker["checked_in"]:
            return
        if not worker["storage_written"]:
            return
        if worker_id not in agent["_completed_ids"]:
            agent["_completed_ids"].add(worker_id)
            agent["total_completed"] += 1

    def ingest_event(self, agent_id: str, event: dict) -> None:
        event_name = event.get("event")
        if not event_name:
            return
        event_name = self._EVENT_ALIASES.get(event_name, event_name)

        event_ts = event.get("t")
        if not event_ts:
            ts_ms = event.get("ts_unix_ms")
            if ts_ms:
                event_ts = float(ts_ms) / 1000.0
        event_ts = float(event_ts or time.time())

        with self._lock:
            agent = self._ensure_agent(agent_id)
            agent["last_event_at"] = event_ts
            self._state["last_event_at"] = event_ts
            self._state["updated_at"] = time.time()

            if event_name == "agent_start":
                agent["status"] = "running"
                agent["worker_backend"] = event.get("worker_backend")
                agent["worker_runtime"] = event.get("worker_runtime")
                max_workers = event.get("max_concurrent_workers")
                if isinstance(max_workers, int):
                    self._state["max_worker_slots"] = max(
                        self._state["max_worker_slots"],
                        max_workers,
                    )
                return

            if event_name == "benchmark_start_signal":
                agent["benchmark_started"] = True
                if agent["status"] == "pending":
                    agent["status"] = "running"
                return

            if event_name == "worker_start":
                worker_id = event.get("worker_id")
                if not worker_id:
                    return
                worker = agent["workers"].get(worker_id)
                if worker is None:
                    worker = _blank_worker(worker_id)
                    agent["workers"][worker_id] = worker
                worker["status"] = "running"
                worker["started_at"] = event_ts
                if worker_id not in agent["_spawned_ids"]:
                    agent["_spawned_ids"].add(worker_id)
                    agent["total_spawned"] += 1
                agent["active_workers"] = sum(
                    1 for w in agent["workers"].values() if not w["exited"]
                )
                if agent["status"] == "pending":
                    agent["status"] = "running"
                return

            if event_name == "heartbeat":
                worker_id = event.get("worker_id")
                if not worker_id:
                    return
                worker = agent["workers"].get(worker_id)
                if worker is not None:
                    worker["rss_kb"] = int(event.get("rss_kb", -1))
                return

            if event_name == "checkin":
                worker_id = event.get("worker_id")
                if not worker_id:
                    return
                # The checkin POST carries a sandbox-generated UUID (real
                # job ID is scrubbed by shell.rs), so correlate to the
                # oldest started-but-unchecked-in worker for this agent.
                worker = agent["workers"].get(worker_id)
                if worker is None:
                    candidates = [
                        w for w in agent["workers"].values()
                        if w["started_at"] and not w["checked_in"]
                    ]
                    if not candidates:
                        return
                    worker = min(candidates, key=lambda w: w["started_at"])
                worker["checked_in"] = True
                worker["checkin_at"] = event_ts
                if event.get("emoji"):
                    worker["checkin_emoji"] = event["emoji"]
                if worker["started_at"] and event_ts:
                    worker["cold_start_ms"] = round(
                        (event_ts - worker["started_at"]) * 1000
                    )
                if worker_id not in agent["_checkin_ids"]:
                    agent["_checkin_ids"].add(worker_id)
                    agent["total_checkins"] += 1
                # Late checkin for an already-exited worker.
                if worker["exited"]:
                    self._maybe_count_clean(agent, worker["id"], worker)
                return

            if event_name == "worker_storage_written":
                worker_id = event.get("worker_id")
                if not worker_id:
                    return
                worker = agent["workers"].get(worker_id)
                if worker is not None:
                    worker["storage_written"] = True
                    if worker["exited"]:
                        self._maybe_count_clean(agent, worker_id, worker)
                if worker_id not in agent["_worker_storage_ids"]:
                    agent["_worker_storage_ids"].add(worker_id)
                    agent["total_worker_storage_written"] += 1
                return

            if event_name == "agent_storage_verified":
                worker_id = event.get("worker_id")
                if not worker_id:
                    return
                if worker_id not in agent["_agent_storage_ids"]:
                    agent["_agent_storage_ids"].add(worker_id)
                    agent["total_agent_storage_verified"] += 1
                return

            if event_name == "worker_end":
                worker_id = event.get("worker_id")
                if worker_id:
                    worker = agent["workers"].get(worker_id)
                    if worker is not None:
                        worker["exited"] = True
                        self._maybe_count_clean(agent, worker_id, worker)
                agent["active_workers"] = sum(
                    1 for w in agent["workers"].values() if not w["exited"]
                )
                return

            if event_name == "status":
                return

            if event_name == "agent_stop":
                agent["status"] = "stopped"
                for w in agent["workers"].values():
                    w["exited"] = True
                agent["active_workers"] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state
            elapsed_s = 0.0
            if state["running_started_at"] is not None:
                elapsed_s = time.time() - state["running_started_at"]
            agents = []
            for agent_id in state["agent_order"]:
                agent = self._ensure_agent(agent_id)
                workers = [
                    {
                        "id": worker["id"],
                        "index": worker["index"],
                        "status": worker["status"],
                        "started_at": worker["started_at"],
                        "checked_in": worker["checked_in"],
                        "checkin_at": worker["checkin_at"],
                        "checkin_emoji": worker["checkin_emoji"],
                        "storage_written": worker["storage_written"],
                        "cold_start_ms": worker["cold_start_ms"],
                        "rss_kb": worker["rss_kb"],
                    }
                    for worker in sorted(
                        agent["workers"].values(),
                        key=lambda worker: worker["index"],
                    )
                    if not worker["exited"]
                ]
                gateways = state.get("agent_gateways", {})
                agents.append(
                    {
                        "id": agent["id"],
                        "index": agent["index"],
                        "status": agent["status"],
                        "benchmark_started": agent["benchmark_started"],
                        "active_workers": agent["active_workers"],
                        "total_spawned": agent["total_spawned"],
                        "total_checkins": agent["total_checkins"],
                        "total_completed": agent["total_completed"],
                        "total_worker_storage_written": agent["total_worker_storage_written"],
                        "total_agent_storage_verified": agent["total_agent_storage_verified"],
                        "worker_backend": agent["worker_backend"],
                        "worker_runtime": agent["worker_runtime"],
                        "last_event_at": agent["last_event_at"],
                        "workers": workers,
                        "gateway_port": gateways.get(agent_id),
                    }
                )

            started_agents = sum(
                1 for agent in agents if agent["status"] in ("running", "stopped")
            )
            benchmark_started_agents = sum(
                1 for agent in agents if agent["benchmark_started"]
            )
            stopped_agents = sum(
                1 for agent in agents if agent["status"] == "stopped"
            )
            active_workers = sum(agent["active_workers"] for agent in agents)
            total_spawned = sum(agent["total_spawned"] for agent in agents)
            total_checkins = sum(agent["total_checkins"] for agent in agents)
            total_completed = sum(agent["total_completed"] for agent in agents)
            total_worker_storage_written = sum(
                agent["total_worker_storage_written"] for agent in agents
            )
            total_agent_storage_verified = sum(
                agent["total_agent_storage_verified"] for agent in agents
            )
            return {
                "approach": state["approach"],
                "mode": state["mode"],
                "run_id": state["run_id"],
                "duration_s": state["duration_s"],
                "elapsed_s": round(elapsed_s, 1),
                "max_worker_slots": state["max_worker_slots"],
                "phase": state["phase"],
                "phase_detail": state["phase_detail"],
                "created_at": state["created_at"],
                "updated_at": state["updated_at"],
                "last_event_at": state["last_event_at"],
                "num_agents": state["expected_agents"],
                "expected_agents": state["expected_agents"],
                "started_agents": started_agents,
                "active_workers": active_workers,
                "total_spawned": total_spawned,
                "total_checkins": total_checkins,
                "total_completed": total_completed,
                "lifecycle": {
                    "agents_started": started_agents,
                    "benchmark_started": benchmark_started_agents,
                    "workers_launched": total_spawned,
                    "successful_checkins": total_checkins,
                    "workers_finished": total_completed,
                    "worker_storage_written": total_worker_storage_written,
                    "agent_storage_verified": total_agent_storage_verified,
                    "agents_stopped": stopped_agents,
                    "active_workers": active_workers,
                },
                "agents": agents,
                "job_params": state["job_params"],
            }

    def _ensure_agent(self, agent_id: str) -> dict:
        agents = self._state["agents"]
        if agent_id not in agents:
            agents[agent_id] = _blank_agent(agent_id)
            if agent_id not in self._state["agent_order"]:
                self._state["agent_order"].append(agent_id)
        return agents[agent_id]


@dataclass
class _LogCursor:
    path: Path
    offset: int = 0
    inode: Optional[int] = None
    partial: str = ""


class EventLogTailer:
    """Poll host-visible JSONL files and apply new events to MonitorState."""

    def __init__(self, state: MonitorState, poll_interval_s: float = 0.5) -> None:
        self._state = state
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._paths: Dict[str, Path] = {}
        self._cursors: Dict[str, _LogCursor] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def update_paths(self, paths: Dict[str, Path]) -> None:
        with self._lock:
            self._paths = {agent_id: Path(path) for agent_id, path in paths.items()}
            stale = set(self._cursors) - set(self._paths)
            for agent_id in stale:
                self._cursors.pop(agent_id, None)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval_s):
            with self._lock:
                paths = copy.copy(self._paths)
            for agent_id, path in paths.items():
                self._poll_path(agent_id, path)

    def _poll_path(self, agent_id: str, path: Path) -> None:
        cursor = self._cursors.get(agent_id)
        if cursor is None or cursor.path != path:
            cursor = _LogCursor(path=path)
            self._cursors[agent_id] = cursor

        try:
            stat = path.stat()
        except FileNotFoundError:
            return

        if cursor.inode != stat.st_ino or stat.st_size < cursor.offset:
            cursor.offset = 0
            cursor.inode = stat.st_ino
            cursor.partial = ""

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(cursor.offset)
                chunk = handle.read()
                cursor.offset = handle.tell()
        except OSError:
            return

        if not chunk:
            return

        payload = cursor.partial + chunk
        if payload.endswith("\n"):
            cursor.partial = ""
            lines = payload.splitlines()
        else:
            lines = payload.splitlines()
            cursor.partial = lines.pop() if lines else payload

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            self._state.ingest_event(event.get("agent_id", agent_id), event)


class _MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


class _MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/state":
            body = json.dumps(self.server.monitor.state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # /agent-loader/<id>/ — service worker loader page
        loader_match = re.match(r"^/agent-loader/(agent-\d+)/?$", self.path)
        if loader_match:
            agent_id = loader_match.group(1)
            base = f"/agent/{agent_id}"
            body = _AGENT_LOADER_HTML.format(agent_id=agent_id, base=base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # /agent/<id>/sw.js — service worker script
        sw_match = re.match(r"^/agent/(agent-\d+)/sw\.js$", self.path)
        if sw_match:
            agent_id = sw_match.group(1)
            base = f"/agent/{agent_id}"
            body = _SW_JS_TEMPLATE.format(base=base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            # Service-Worker-Allowed header lets the SW control its scope
            self.send_header("Service-Worker-Allowed", base + "/")
            self.end_headers()
            self.wfile.write(body)
            return

        # /agent/<id>/... — reverse proxy to agent gateway
        if self._proxy_agent("GET"):
            return

        self.send_error(404)

    def do_POST(self) -> None:
        if self._proxy_agent("POST"):
            return
        self.send_error(404)

    def _proxy_agent(self, method: str) -> bool:
        match = _AGENT_PROXY_RE.match(self.path)
        if not match:
            return False
        agent_id = match.group(1)
        inner_path = match.group(2) or "/"
        gateways = self.server.monitor.state.get_agent_gateways()
        port = gateways.get(agent_id)
        if not port:
            self.send_error(502, f"No gateway for {agent_id}")
            return True

        url = f"http://127.0.0.1:{port}{inner_path}"
        body = None
        if method == "POST":
            cl = int(self.headers.get("Content-Length", 0))
            if cl > 0:
                body = self.rfile.read(cl)

        req = urllib.request.Request(url, data=body, method=method)
        for h in ("Authorization", "Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)

        try:
            resp = urllib.request.urlopen(req, timeout=15)
            ct = resp.headers.get("Content-Type", "application/octet-stream")

            # Stream SSE responses without buffering
            if "text/event-stream" in ct:
                self.send_response(resp.status)
                self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return True

            resp_body = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            rb = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "text/plain"))
            self.send_header("Content-Length", str(len(rb)))
            self.end_headers()
            self.wfile.write(rb)
        except Exception as e:
            rb = f"Proxy error: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(rb)))
            self.end_headers()
            self.wfile.write(rb)
        return True

    def log_message(self, format: str, *args) -> None:
        return


class BenchmarkMonitor:
    """Serve and update a live benchmark topology page."""

    def __init__(
        self,
        host: str,
        port: int,
        approach: str,
        mode: str,
        run_id: str,
        expected_agents: List[str],
        duration_s: int,
        max_worker_slots: int,
    ) -> None:
        self.state = MonitorState(
            approach=approach,
            mode=mode,
            run_id=run_id,
            expected_agents=expected_agents,
            duration_s=duration_s,
            max_worker_slots=max_worker_slots,
        )
        self._tailer = EventLogTailer(self.state)
        self._server = _MonitorHTTPServer((host, port), _MonitorHandler)
        self._server.monitor = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"http://{display_host}:{port}/"

    def start(self) -> None:
        self._tailer.start()
        self._thread.start()

    def stop(self) -> None:
        self._tailer.stop()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def set_phase(self, phase: str, detail: str = "") -> None:
        self.state.set_phase(phase, detail)

    def attach_agents(self, agent_ids: List[str], log_paths: Dict[str, Path]) -> None:
        self.state.attach_agents(agent_ids)
        self._tailer.update_paths(log_paths)
