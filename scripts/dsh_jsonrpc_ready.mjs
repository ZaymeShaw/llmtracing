// Launch the unmodified DSH plugin tree and publish readiness only after boot
// settles. The SDK transport may accept initialize before sibling MCP plugins
// finish discovery; callers must wait for this marker before sending prompts.
import { writeFileSync, renameSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const readyPath = process.env.DSH_READY_FILE
function publish(value) {
  const temp = `${readyPath}.tmp`
  writeFileSync(temp, JSON.stringify(value))
  renameSync(temp, readyPath)
}

try {
  const { boot, installFailLoud } = await import(pathToFileURL(process.env.DSH_BOOT_MODULE).href)
  installFailLoud('dsh-lane-sdk')
  const ctx = await boot('dsh-lane-sdk', process.env.DSH_CORDIS_CONFIG)
  let exiting = false
  async function exit(code) {
    if (exiting) return
    exiting = true
    try { await ctx.fiber.dispose() } finally { process.exit(code) }
  }
  process.stdin.on('end', () => { void exit(0) })
  process.on('SIGTERM', () => { void exit(0) })
  process.on('SIGINT', () => { void exit(130) })
  const tools = ctx.get('tools')?.schemas().map(tool => tool.name) ?? []
  if (process.env.DSH_REQUIRE_INSURANCE_MCP === '1' &&
      (!tools.length || tools.some(name => !name.startsWith('mcp__insurance-tools__')))) {
    publish({ ready: false, error: 'Expected insurance MCP tools only after boot', tools })
    await exit(1)
  }
  publish({ ready: true, tools })
} catch (error) {
  publish({ ready: false, error: String(error) })
  process.exit(1)
}
