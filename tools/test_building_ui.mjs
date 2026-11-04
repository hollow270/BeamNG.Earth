import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ui = path.join(root, 'mod/beamng_earth/ui/ui-vue/mods/beamngEarth')
const code = fs.readFileSync(path.join(ui, 'buildingRefresh.js'), 'utf8')
const model = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
if (process.argv[2] === '--command') {
  const request = JSON.parse(process.argv[3])
  process.stdout.write(model.commandScript(request.op, request.payload))
  process.exit(0)
}
let checks = 0
function check(fn) { fn(); checks++ }
const box = { w: 0, s: 0, e: 0.001, n: 0.001 }
check(() => assert.equal(model.selection(box).error, ''))
for (const bad of [null, { ...box, e: 1 }, { ...box, n: 1 }, { ...box, w: true }, { ...box, n: Infinity },
  { ...box, w: 1 }, { ...box, s: -90 }, { ...box, n: 0 }, { w: 179, s: 0, e: -179, n: 1 }]) {
  check(() => assert.ok(model.selection(bad).error))
}
const proposal = { available: true, state: 'awaiting_acceptance', preview: { token: 'a'.repeat(64), selection: [0, 0, .001, .001] } }
check(() => assert.equal(model.canAccept(proposal, box), true))
check(() => assert.equal(model.canAccept({ ...proposal, state: 'fetching' }, box), false))
check(() => assert.equal(model.canAccept(proposal, { ...box, e: .002 }), false))
check(() => assert.throws(() => model.commandScript('exec', {})))
check(() => assert.throws(() => model.commandScript('preview', { endpoint: 'https://not-allowed.invalid' })))
check(() => assert.throws(() => model.commandScript('accept', { preview_token: 'x'.repeat(2100) })))
check(() => assert.ok(!model.commandScript('preview', { bounds: [0, 0, .001, .001] }).includes('authorization')))
const require = createRequire(import.meta.url)
const compiler = require(path.join(root, '.venv/overlay-ui/node_modules/@vue/compiler-sfc'))
for (const name of ['EarthMap.vue', 'EarthBuildingRefresh.vue', 'EarthSettingsPage.vue', 'EarthSettingsBody.vue']) {
  check(() => {
    const filename = path.join(ui, name)
    const parsed = compiler.parse(fs.readFileSync(filename, 'utf8'), { filename })
    assert.deepEqual(parsed.errors, [], name)
    const script = compiler.compileScript(parsed.descriptor, { id: name })
    const template = compiler.compileTemplate({ source: parsed.descriptor.template.content, filename,
      id: name, compilerOptions: { bindingMetadata: script.bindings } })
    assert.deepEqual(template.errors, [], name)
  })
}
const { mountBuildingPanel } = await import('./building_ui_render.mjs')
const counts = { addition: 1, replacement: 1, unchanged: 2, conflict: 0 }
const review = { ...proposal, fetch_available: true, command_pending: false, command_id: 0,
  preview: { ...proposal.preview, replication_timestamp: '2026-10-30T12:00:00Z', counts,
    building_counts: counts, part_counts: { addition: 0, replacement: 0, unchanged: 0, conflict: 0 },
    overture_only_count: 0, issue_count: 0, issues: {}, overture_only: {},
    decisions: [{ action: 'unchanged', baseline_ids: {}, reason: 'same_geometry' }],
    changes: [{ feature_id: 'osm:way:9002', attributes: {}, parts_before: 0, parts_after: 0 }] },
  deferred: {}, resources: { uncertain: {}, removed_generations: {}, reclaimed_slots: {}, released_sessions: {} } }
async function renderCheck(fn) { await fn(); checks++ }
await renderCheck(async () => {
  const panel = await mountBuildingPanel(review, box)
  try {
    assert.deepEqual(panel.errors, [])
    assert.match(panel.text(), /attributes: unchanged/)
    assert.match(panel.text(), /Overture-only: none/)
    assert.match(panel.text(), /Unchanged records2/)
    assert.equal(panel.button('Accept and apply').props.disabled, false)
    assert.equal(panel.commands.length, 0, 'mounting or polling must never accept')
    await panel.update(review); assert.equal(panel.commands.length, 0)
    await panel.click('Accept and apply')
    assert.equal(panel.commands.length, 1)
    assert.match(panel.commands[0], /uiBuildingsCommand\("accept"/)
    assert.ok(panel.commands[0].includes('\\"confirm\\":true'))
  } finally { panel.close() }
})
for (const patch of [null, [], 'bad', { preview: 42 }, { preview: { ...review.preview, changes: [null, 'bad', {}] } },
  { preview: { ...review.preview, changes: [{ feature_id: {}, attributes: 'height' }], issues: [false], overture_only: 7 } },
  { preview: { ...review.preview, decisions: [{ baseline_ids: 'bad' }] } },
  { resources: { uncertain: [null, {}], removed_generations: 8, reclaimed_slots: null, released_sessions: 'bad' }, deferred: [null, 4, { key: [] }] },
  { error: { message: {}, retry_after: 'bad' }, accepted_revision: 42, state: {} },
  { preview: { ...review.preview, counts: {}, selection: {} } }]) {
  await renderCheck(async () => {
    const value = patch && !Array.isArray(patch) && typeof patch === 'object' ? { ...review, ...patch } : patch
    const panel = await mountBuildingPanel(value, box)
    try {
      assert.deepEqual(panel.errors, [], JSON.stringify(patch))
      assert.equal(panel.commands.length, 0)
      if (panel.button('Accept and apply')) assert.equal(panel.button('Accept and apply').props.disabled, true)
    } finally { panel.close() }
  })
}
await renderCheck(async () => {
  const panel = await mountBuildingPanel(review, box)
  try {
    await panel.click('Fetch and preview')
    await panel.click('Fetch and preview', true)
    assert.equal(panel.commands.length, 1, 'double-click before bridge receipt must be blocked')
    await panel.receipt()
    await panel.update({ ...review, command_id: 0, command_pending: false })
    await panel.click('Fetch and preview', true)
    assert.equal(panel.commands.length, 1, 'an older poll cannot complete this request')
    await panel.update({ ...review, command_id: 1, command_pending: true, operation_pending: true, state: 'fetching' })
    assert.equal(panel.button('Accept and apply').props.disabled, true)
    await panel.update({ ...review, command_id: 1, command_pending: false, error: { message: 'Request spacing is still in effect.' } })
    assert.equal(panel.button('Accept and apply').props.disabled, false)
    assert.match(panel.text(), /attributes: unchanged/)
    assert.match(panel.text(), /Request spacing/)
    assert.equal(panel.commands.length, 1, 'a retained review never causes automatic acceptance')
    assert.deepEqual(panel.errors, [])
  } finally { panel.close() }
})
await renderCheck(async () => {
  const panel = await mountBuildingPanel({ ...review, operation_pending: true }, box)
  try {
    await panel.click('Fetch and preview', true)
    assert.equal(panel.commands.length, 0, 'reopened panel respects the Lua single-flight guard')
    assert.equal(panel.button('Accept and apply').props.disabled, true)
  } finally { panel.close() }
})
for (const restoration of [{ mode: 'waiting', code: 'trackers_settling', message: 'Waiting for the new anchor.' },
  { mode: 'baseline', code: 'baseline_active', message: 'Original building baseline is active.' },
  { mode: 'blocked', code: 'artifacts_invalid', message: 'Stop and inspect local diagnostics before driving.' }]) {
  await renderCheck(async () => {
    const panel = await mountBuildingPanel({ ...review, restoration }, box)
    try {
      assert.deepEqual(panel.errors, [])
      assert.ok(panel.text().includes(restoration.message)); assert.ok(panel.text().includes(restoration.code))
      assert.equal(panel.commands.length, 0, 'restoration status must never trigger a command or acceptance')
    } finally { panel.close() }
  })
}
await renderCheck(async () => {
  const panel = await mountBuildingPanel({ ...review, restoration: { mode: {}, code: [], message: {} } }, box)
  try { assert.deepEqual(panel.errors, []); assert.equal(panel.commands.length, 0) } finally { panel.close() }
})
await renderCheck(async () => {
  const progress = { required: 312, acknowledged: 96, pending: 216, blocked: 0, components: 22, remaining_components: 15, oversized: 0 }
  const panel = await mountBuildingPanel({ ...review, state: 'activation_deferred', progress, deferred_overflow: true,
    deferred: [{ kind: 'far_geo', key: 'g17_1_1', reason: 'Waiting for complete group' }] }, box)
  try {
    assert.deepEqual(panel.errors, [])
    assert.match(panel.text(), /96 \/ 312 current targets acknowledged; 216 pending/)
    assert.match(panel.text(), /At least 1 dependent targets deferred/)
    assert.equal(panel.commands.length, 0, 'partial progress never accepts or retries automatically')
    await panel.update({ ...review, progress: { ...progress, acknowledged: 312, pending: 0, remaining_components: 0 }, state: 'activated' })
    assert.match(panel.text(), /312 \/ 312 current targets acknowledged; 0 pending/)
    assert.equal(panel.commands.length, 0)
  } finally { panel.close() }
})
for (const progress of ['bad', {}, { required: 312, acknowledged: 400, pending: 0 },
  { required: 313, acknowledged: 0, pending: 312, blocked: 0, components: 1, remaining_components: 1, oversized: 0 }]) {
  check(() => {
    const normalized = model.normalizeBuildingStatus({ progress })
    assert.equal(normalized.progress, null)
    assert.ok(normalized.data_warning)
  })
}
console.log(`${checks} UI checks passed (bridge validation, four Vue compilations and real Vue runtime rendering/events).`)
