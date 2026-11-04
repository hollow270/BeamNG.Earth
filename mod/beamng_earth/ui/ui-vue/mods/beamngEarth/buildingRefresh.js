// Pure validation and bridge encoding, shared with the repository-local Node checks.
export const buildingStates = {
  unavailable: "Unavailable", idle: "Ready", fetching: "Fetching OSM buildings",
  preview_available: "Preview available", awaiting_acceptance: "Awaiting your acceptance",
  accepting: "Saving your accepted preview", accepted: "Accepted locally",
  preparing: "Preparing building meshes", prepared: "Meshes prepared",
  activation_scheduled: "Activation scheduled", activation_deferred: "Activation deferred",
  activating: "Installing buildings and retiring old collision", activated: "Activated in the current view",
  failed: "Operation failed", superseded: "World or revision changed", retiring: "Retiring resources",
}
const ops = new Set(["status", "preview", "accept", "retry", "disable", "enable", "rollback", "cleanup"])
export function selection(box) {
  if (!box || ![box.w, box.s, box.e, box.n].every(v => typeof v === "number" && Number.isFinite(v))) {
    return { error: "Choose a small area on the map." }
  }
  const { w, s, e, n } = box
  if (!(w >= -180 && w < e && e <= 180 && s >= -85 && s < n && n <= 85)) return { error: "Invalid geographic bounds." }
  const latitude = s <= 0 && n >= 0 ? 0 : Math.min(Math.abs(s), Math.abs(n))
  const width = (e - w) * 111320 * Math.cos(latitude * Math.PI / 180), height = (n - s) * 111320
  return { bounds: [w, s, e, n], width, height,
    error: width > 500 || height > 500 ? "Select at most 500 × 500 metres. Zoom in and choose a smaller area." : "" }
}
export function sameSelection(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === 4 && b.length === 4 &&
    a.every((v, i) => typeof v === "number" && Number.isFinite(v) && typeof b[i] === "number" && Number.isFinite(b[i]) && Math.abs(v - b[i]) <= 1e-9)
}
export function commandScript(op, payload = {}) {
  if (!ops.has(op) || !payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Invalid building command")
  if (Object.keys(payload).some(k => !["bounds", "preview_token", "confirm", "expected_state"].includes(k))) throw new Error("Unsupported request field")
  const text = JSON.stringify(payload)
  if (text.length > 2048 || /[^\x20-\x7e]/.test(text)) throw new Error("Building request exceeds bridge limits")
  return `beamngEarth_settings and beamngEarth_settings.uiBuildingsCommand(${JSON.stringify(op)}, ${JSON.stringify(text)}) or nil`
}
export function canAccept(state, box) {
  return state?.available === true && state.state === "awaiting_acceptance" &&
    !state.operation_pending && !state.command_pending && state.preview?.review_valid !== false &&
    typeof state.preview?.token === "string" && /^[a-f0-9]{64}$/.test(state.preview.token) && sameSelection(state.preview.selection, selection(box).bounds)
}
export function revisionText(value) { return typeof value === "string" && value ? value.slice(0, 12) : "Original Overture baseline" }

// JSON [] becomes an untyped empty Lua table; the engine bridge can encode it as {}.
// Normalize only the public view, never the private capability or server proposal.
export function normalizeBuildingStatus(value) {
  const object = v => v !== null && typeof v === "object" && !Array.isArray(v)
  let malformed = false
  const text = (v, fallback = "") => {
    if (v == null) return fallback
    if (typeof v !== "string") { malformed = true; return fallback }
    if (v.length > 4096) malformed = true
    return v.slice(0, 4096)
  }
  const count = (v, required = false) => {
    if (v == null && !required) return 0
    if (!Number.isSafeInteger(v) || v < 0) { malformed = true; return 0 }
    return v
  }
  const list = (v, limit = 128) => {
    if (v == null || (object(v) && Object.keys(v).length === 0)) return []
    if (!Array.isArray(v)) { malformed = true; return [] }
    if (v.length > limit) malformed = true
    return v.slice(0, limit)
  }
  const strings = (v, limit) => list(v, limit).flatMap(v => {
    if (typeof v !== "string") { malformed = true; return [] }
    return [text(v)]
  })
  const rows = (v, project, limit) => list(v, limit).flatMap(v => {
    if (!object(v)) { malformed = true; return [] }
    return [project(v)]
  })
  const counts = v => Object.fromEntries(["addition", "replacement", "unchanged", "conflict"].map(k => [k, count(object(v) ? v[k] : null, true)]))
  const raw = object(value) ? value : {}
  if (!object(value)) malformed = true
  const out = {
    available: raw.available === true, fetch_available: raw.fetch_available === true,
    state: text(raw.state, "unavailable"), reason: text(raw.reason), detail: text(raw.detail), fetch_reason: text(raw.fetch_reason),
    enabled: raw.enabled === true, can_rollback: raw.can_rollback === true,
    accepted_revision: text(raw.accepted_revision), activated_revision: text(raw.activated_revision),
    accepted_state: text(raw.accepted_state), activated_enabled: raw.activated_enabled,
    held_slots: count(raw.held_slots), operation_pending: raw.operation_pending === true,
    command_pending: raw.command_pending === true, command_id: raw.command_id,
    preview: null, error: null, resources: null, restoration: null, progress: null,
    deferred_overflow: raw.deferred_overflow === true,
    deferred: rows(raw.deferred, r => ({ kind: text(r.kind), key: text(r.key), reason: text(r.reason) })),
  }
  if (raw.progress != null) {
    if (!object(raw.progress)) malformed = true
    else {
      const keys = ["required", "acknowledged", "pending", "blocked", "components", "remaining_components", "oversized"]
      const p = Object.fromEntries(keys.map(k => [k, count(raw.progress[k], true)]))
      if (keys.some(k => !Number.isSafeInteger(raw.progress[k]) || raw.progress[k] < 0 || raw.progress[k] > 512) || p.acknowledged + p.pending !== p.required || p.blocked > p.pending || p.oversized > p.blocked || p.remaining_components > p.components) malformed = true
      else out.progress = p
    }
  }
  if (raw.error != null) out.error = { message: text(object(raw.error) ? raw.error.message : raw.error, "Invalid error response"),
    retry_after: object(raw.error) && typeof raw.error.retry_after === "number" && Number.isFinite(raw.error.retry_after) ? Math.max(0, Math.min(86400, raw.error.retry_after)) : 0 }
  if (object(raw.restoration)) out.restoration = { mode: text(raw.restoration.mode), code: text(raw.restoration.code), message: text(raw.restoration.message) }
  if (raw.preview != null) {
    const p = object(raw.preview) ? raw.preview : {}
    if (!object(raw.preview)) malformed = true
    out.preview = {
      token: text(p.token), selection: list(p.selection, 4), replication_timestamp: text(p.replication_timestamp),
      counts: counts(p.counts), building_counts: counts(p.building_counts), part_counts: counts(p.part_counts),
      overture_only_count: count(p.overture_only_count, true), issue_count: count(p.issue_count, true),
      enabled_before: p.enabled_before === true, details_truncated: p.details_truncated === true,
      issues: rows(p.issues, r => ({ feature_id: text(r.feature_id), code: text(r.code) })),
      decisions: rows(p.decisions, r => ({ action: text(r.action), incoming_id: text(r.incoming_id),
        baseline_ids: strings(r.baseline_ids), reason: text(r.reason), geometry_changed: r.geometry_changed === true })),
      changes: rows(p.changes, r => ({ feature_id: text(r.feature_id), geometry_changed: r.geometry_changed === true,
        attributes: strings(r.attributes, 24), parts_before: count(r.parts_before), parts_after: count(r.parts_after) }), 64),
      overture_only: strings(p.overture_only, 64),
    }
    if (!/^[a-f0-9]{64}$/.test(out.preview.token) || !sameSelection(out.preview.selection, out.preview.selection)) malformed = true
    out.preview.review_valid = !malformed
  }
  if (raw.resources != null) {
    const r = object(raw.resources) ? raw.resources : {}
    if (!object(raw.resources)) malformed = true
    out.resources = { held: count(r.held), uncertain: strings(r.uncertain),
      removed_generations: list(r.removed_generations), reclaimed_slots: list(r.reclaimed_slots), released_sessions: list(r.released_sessions) }
  }
  out.data_warning = malformed ? "Some companion details were malformed. Invalid details were omitted; obtain a valid review before accepting." : ""
  if (malformed && out.preview) out.preview.review_valid = false
  return out
}
