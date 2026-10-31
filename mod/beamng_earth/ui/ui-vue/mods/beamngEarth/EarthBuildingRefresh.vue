<template>
  <section class="building-refresh" @mousedown.stop @wheel.stop @dblclick.stop>
    <div class="building-heading"><strong>Refresh buildings from OSM</strong><button class="map-btn small" @click="$emit('close')">Close</button></div>
    <p>Updates building footprints and attributes. Roads and terrain use the original Overture baseline.</p>
    <p v-if="!status.available" class="building-warning">{{ status.reason || "Not configured for this local game." }}</p>
    <p v-else>Manual fetching only. Existing saved overlays can prepare locally as you drive; driving never fetches fresh OSM data.</p>
    <button class="map-btn" @click="$emit('pick')">Choose building refresh area</button>
    <template v-if="area.bounds">
      <p>{{ Math.round(area.width) }} × {{ Math.round(area.height) }} metres (approx.)</p>
      <p class="building-bounds">W {{ bounds.w.toFixed(6) }}, S {{ bounds.s.toFixed(6) }}<br>E {{ bounds.e.toFixed(6) }}, N {{ bounds.n.toFixed(6) }}</p>
    </template>
    <p v-if="area.error" class="building-warning">{{ area.error }}</p>
    <p v-if="status.fetch_reason" class="building-warning">{{ status.fetch_reason }}</p>
    <button class="map-btn primary" :disabled="!status.fetch_available || !!area.error || busy" @click="request('preview', { bounds: area.bounds })">Fetch and preview buildings</button>
    <p class="building-state" role="status">{{ buildingStates[status.state] || status.state || "Waiting for companion app" }}</p>
    <p v-if="status.detail">{{ status.detail }}</p>
    <p v-if="status.error || error" class="building-warning" role="alert">{{ error || status.error.message }}<span v-if="status.error?.retry_after"> Retry after at least {{ Math.ceil(status.error.retry_after) }} seconds.</span></p>
    <div v-if="status.preview" class="building-review">
      <strong>Review the proposed changes</strong>
      <p>OSM replication snapshot: {{ status.preview.replication_timestamp }}</p>
      <dl>
        <dt>New OSM buildings</dt><dd>{{ status.preview.building_counts.addition }}</dd>
        <dt>Safely matched / updated buildings</dt><dd>{{ status.preview.building_counts.replacement }}</dd>
        <dt>New / updated building parts</dt><dd>{{ status.preview.part_counts.addition }} / {{ status.preview.part_counts.replacement }}</dd>
        <dt>Unchanged records</dt><dd>{{ status.preview.counts.unchanged }}</dd>
        <dt>Conflicts retained for review</dt><dd>{{ status.preview.counts.conflict }}</dd>
        <dt>Overture-only buildings retained</dt><dd>{{ status.preview.overture_only_count }}</dd>
        <dt>Source / unsupported geometry issues</dt><dd>{{ status.preview.issue_count }}</dd>
      </dl>
      <p>No absent OSM feature is treated as a deletion. Conflicts and unsupported geometry keep the conservative merge rules.</p>
      <ul v-if="status.preview.issues.length"><li v-for="(issue, i) in status.preview.issues" :key="i">{{ issue.feature_id || 'Source' }}: {{ issue.code }}</li></ul>
      <details><summary>Changes, conflicts and retained originals</summary>
        <ul><li v-for="(row, i) in status.preview.decisions" :key="i">{{ row.action }} — {{ row.incoming_id || row.baseline_ids.join(', ') }}: {{ row.reason }}<span v-if="row.geometry_changed">; geometry changed</span></li></ul>
        <ul><li v-for="row in status.preview.changes" :key="row.feature_id">{{ row.feature_id }}: {{ row.geometry_changed ? 'footprint changed' : 'footprint unchanged' }}; attributes: {{ row.attributes.join(', ') || 'unchanged' }}; parts {{ row.parts_before }} → {{ row.parts_after }}</li></ul>
        <p>Overture-only: {{ status.preview.overture_only.join(', ') || 'none' }}</p>
        <p v-if="status.preview.details_truncated">Detail list shortened for this view; the counts cover the full proposal.</p>
      </details>
      <p v-if="!matchingPreview" class="building-warning">The selected area changed. Fetch a new preview before accepting.</p>
      <button class="map-btn primary" :disabled="!matchingPreview || busy" @click="request('accept', { preview_token: status.preview.token, confirm: true })">Accept and apply these buildings</button>
      <p v-if="!status.preview.enabled_before">Accepting also enables your saved building overlays.</p>
    </div>
    <p>Accepted revision: <span :title="status.accepted_revision">{{ revisionText(status.accepted_revision) }}</span><br>
       Activated revision: <span :title="status.activated_revision">{{ status.activated_enabled === false ? 'Original Overture baseline' : status.activated_revision ? revisionText(status.activated_revision) : 'Not confirmed for the current view' }}</span></p>
    <p v-if="status.state !== 'activated'">Saved data and prepared meshes are not confirmation that the game has activated them.</p>
    <details v-if="status.deferred?.length" open><summary>{{ status.deferred.length }} dependent targets deferred</summary><ul><li v-for="row in status.deferred" :key="row.kind + row.key">{{ row.kind }} {{ row.key }}: {{ row.reason }}</li></ul></details>
    <p v-if="status.resources?.uncertain?.length" class="building-warning">Some resources are retained because their release cannot be proven. {{ status.resources.uncertain.length }} retention notes.</p>
    <details v-if="status.resources"><summary>Resource inspection</summary>
      <p>Held activation slots: {{ status.resources.held }}. Reclaimed generations: {{ status.resources.removed_generations.length }}; pooled files: {{ status.resources.reclaimed_slots.length }}; released sessions: {{ status.resources.released_sessions.length }}.</p>
      <ul><li v-for="(reason, i) in status.resources.uncertain" :key="i">{{ reason }}</li></ul>
    </details>
    <p v-if="status.held_slots">{{ status.held_slots }} activation shape slots remain held.</p>
    <div class="building-actions">
      <button class="map-btn small" :disabled="!status.available || busy" @click="request('retry')">Retry locally (no OSM fetch)</button>
      <button class="map-btn small" :disabled="!status.available || busy" @click="confirm = status.enabled ? 'disable' : 'enable'">{{ status.enabled ? 'Disable all overlays' : 'Enable saved overlays' }}</button>
      <button class="map-btn small" :disabled="!status.available || !status.can_rollback || busy" @click="confirm = 'rollback'">Rollback last accepted revision</button>
      <button class="map-btn small" :disabled="!status.available || busy" @click="confirm = 'cleanup'">Inspect / clean released resources</button>
    </div>
    <div v-if="confirm" class="building-review">
      <p>{{ confirmText[confirm] }}</p>
      <button class="map-btn primary" @click="confirmed">Confirm {{ confirm }}</button>
      <button class="map-btn" @click="confirm = null">Cancel</button>
    </div>
    <p class="building-footnote">Buildings may include © OpenStreetMap contributors, ODbL-1.0. Actual BeamNG engine collision readiness remains unverified until Stage 6 testing.</p>
  </section>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue"
import { selection, commandScript, canAccept, buildingStates, revisionText } from "./buildingRefresh.js"
const props = defineProps({ bounds: { type: Object, default: null } })
defineEmits(["pick", "close"])
const status = ref({ available: false, state: "unavailable" }), error = ref(""), confirm = ref(null)
const area = computed(() => selection(props.bounds))
const matchingPreview = computed(() => canAccept(status.value, props.bounds))
const busy = computed(() => ["fetching", "accepting", "preparing", "activating"].includes(status.value.state))
const confirmText = {
  disable: "Disable saved overlays and safely restore original Overture buildings. Stored OSM data is kept.",
  enable: "Enable saved overlays and prepare/activate them locally without fetching OSM.",
  rollback: "Restore the previous validated store revision, then prepare and activate it safely.",
  cleanup: "Inspect ownership and reclaim only resources whose release can be proven. Uncertain resources stay retained.",
}
let timer, alive = true
function engine(script, cb) {
  if (!window.bngApi?.engineLua) { error.value = "BeamNG UI bridge is unavailable"; return }
  window.bngApi.engineLua(script, cb)
}
function poll() {
  engine("beamngEarth_settings and beamngEarth_settings.uiBuildingsState() or nil", value => {
    if (alive && value) status.value = value
  })
}
function request(op, payload = {}) {
  error.value = ""
  engine(commandScript(op, payload), result => {
    if (!alive) return
    if (!result || result.error) error.value = result?.error || "Companion app did not accept the request"
    poll()
  })
}
function confirmed() {
  const op = confirm.value; confirm.value = null
  request(op, { confirm: true, expected_state: status.value.accepted_state })
}
onMounted(() => { poll(); timer = setInterval(poll, 500) })
onUnmounted(() => { alive = false; clearInterval(timer) })
</script>

<style scoped>
.building-refresh { position: absolute; z-index: 12; top: 60px; right: 12px; width: 410px; max-width: calc(100% - 24px); max-height: calc(100% - 125px); overflow-y: auto; padding: 14px; background: rgba(20,22,26,.96); color: #eee; border: 1px solid #65717e; border-radius: 6px; font-size: 13px; }
.building-heading, .building-actions { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.building-refresh p { margin: 8px 0; } .building-warning { color: #ffbb80; } .building-state { font-weight: 600; }
.building-review { border-top: 1px solid #65717e; margin-top: 12px; padding-top: 10px; } .building-review dl { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; } .building-review dd { margin: 0; }
.building-bounds, .building-footnote { font-size: 11px; color: #bec7d0; } .building-refresh ul { padding-left: 20px; max-height: 160px; overflow-y: auto; }
.map-btn { cursor: pointer; border: 1px solid #6b7784; border-radius: 4px; background: #29313a; color: white; padding: 6px 10px; } .map-btn.primary { background: #2165a4; } .map-btn:disabled { opacity: .45; cursor: default; } .map-btn.small { font-size: 12px; }
</style>
