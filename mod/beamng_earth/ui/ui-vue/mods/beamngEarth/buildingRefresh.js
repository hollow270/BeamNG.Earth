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
  return Array.isArray(a) && Array.isArray(b) && a.length === 4 && b.length === 4 && a.every((v, i) => Math.abs(v - b[i]) <= 1e-9)
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
    typeof state.preview?.token === "string" && sameSelection(state.preview.selection, selection(box).bounds)
}
export function revisionText(value) { return value ? value.slice(0, 12) : "Original Overture baseline" }
