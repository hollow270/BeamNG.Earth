// Execute the compiled SFC with the real pinned Vue runtime and an in-memory host.
// No browser/game APIs are mocked inside Vue itself. Only host nodes and engineLua
// are supplied, so expressions, reactivity, events and render errors really run.
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
const require = createRequire(import.meta.url)
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dependency = name => require(path.join(root, '.venv/overlay-ui/node_modules', name))
const compiler = dependency('@vue/compiler-sfc'), Vue = dependency('@vue/runtime-dom')
const ui = process.env.BNGE_UI_TEST_SOURCE_DIR || path.join(root, 'mod/beamng_earth/ui/ui-vue/mods/beamngEarth')
const model = await import('data:text/javascript;base64,' + fs.readFileSync(path.join(ui, 'buildingRefresh.js')).toString('base64'))
const parsed = compiler.parse(fs.readFileSync(path.join(ui, 'EarthBuildingRefresh.vue'), 'utf8'))
let source = compiler.compileScript(parsed.descriptor, { id: 'building-runtime', inlineTemplate: true }).content
source = source.replace(/import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["'];?/g, (_, fields, from) =>
  `const {${fields.replace(/\bas\b/g, ':')}} = ${from === 'vue' ? 'Vue' : 'model'};`)
source = source.replace('export default', 'return')

export async function mountBuildingPanel(initial, bounds) {
  let status = initial, timer, commandId = 0
  const commands = [], errors = [], pendingCallbacks = []
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
  const detach = child => { if (child.parent) { const list = child.parent.children; list.splice(list.indexOf(child), 1) }; child.parent = null }
  const renderer = Vue.createRenderer({
    createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
    setText: (n, text) => { n.text = text }, setElementText: (n, text) => { n.text = text; n.children = [] },
    parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
    patchProp: (n, key, old, value) => { n.props[key] = value }, remove: detach,
    insert: (n, parent, anchor = null) => { detach(n); n.parent = parent; const i = parent.children.indexOf(anchor); parent.children.splice(i < 0 ? parent.children.length : i, 0, n) },
  })
  const bridge = { bngApi: { engineLua(script, cb) {
    if (script.includes('uiBuildingsCommand(')) { commands.push(script); pendingCallbacks.push(() => cb({ id: ++commandId })) }
    else cb(structuredClone(status))
  } } }
  const component = new Function('Vue', 'model', 'window', 'setInterval', 'clearInterval', source)(
    Vue, model, bridge, fn => { timer = fn; return 1 }, () => { timer = null })
  const tree = node('root'), app = renderer.createApp(component, { bounds })
  app.config.errorHandler = e => errors.push(e.message)
  app.mount(tree); await Vue.nextTick()
  const all = (n = tree) => [n, ...n.children.flatMap(child => all(child))]
  const text = n => (n.type === '#comment' ? '' : n.text) + n.children.map(text).join('')
  const button = label => all().find(n => n.type === 'button' && text(n).includes(label))
  return {
    commands, errors, text: () => text(tree), button,
    async click(label, force = false) { const n = button(label); if (!n) throw Error('Missing button: ' + label); if (!force && n.props.disabled) throw Error('Disabled button: ' + label); n.props.onClick(); await Vue.nextTick() },
    async receipt() { pendingCallbacks.splice(0).forEach(fn => fn()); await Vue.nextTick() },
    async update(value) { status = value; timer?.(); await Vue.nextTick() },
    close() { app.unmount() },
  }
}

if (process.argv[2] === '--render-stdin') {
  const cases = JSON.parse(fs.readFileSync(0, 'utf8')), result = []
  for (const value of cases) {
    const panel = await mountBuildingPanel(value.status, value.bounds)
    result.push({ text: panel.text(), errors: panel.errors, accept_disabled: panel.button('Accept and apply')?.props.disabled, commands: panel.commands.length })
    panel.close()
  }
  process.stdout.write(JSON.stringify(result))
}
