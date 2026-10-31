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
console.log(`${checks} UI checks passed (pure bridge behavior and four compiled Vue components).`)
