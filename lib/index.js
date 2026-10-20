/**
 * dsh-codex-cua — bring Codex computer use into DeepSeek Harness.
 *
 * On mount, this plugin installs the `codex-cua` skill into the active DSH
 * home (~/.dsh/skills/codex-cua) together with its runtime client and docs.
 * From then on the agent can load the skill and drive Chrome and native macOS
 * apps through the Codex computer-use runtime.
 *
 * Design notes:
 *  - Never throw out of apply(); a failure must not break profile composition.
 *  - The skill is written idempotently and only replaced when its content
 *    actually changes, so re-mounts are cheap and do not churn mtimes.
 *  - Removal of the plugin does not delete the skill: the files are useful on
 *    their own, and deleting user data on uninstall is surprising.
 */
import { mkdirSync, writeFileSync, readFileSync, existsSync, copyFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-codex-cua'

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG_ROOT = resolve(HERE, '..')
const ASSETS = join(PKG_ROOT, 'assets')

/** DSH home: honours DSH_HOME, otherwise ~/.dsh. */
function dshHome() {
  const explicit = process.env.DSH_HOME
  if (explicit && explicit.trim()) return explicit
  return join(homedir(), '.dsh')
}

const FILES = [
  ['SKILL.md', 'SKILL.md'],
  ['cuad.py', 'cuad.py'],
  ['docs/pitfalls.md', 'docs/pitfalls.md'],
  ['docs/protocol.md', 'docs/protocol.md'],
]

/**
 * Copy one asset into the skill directory if missing or changed.
 * @returns {'written' | 'unchanged' | 'missing-asset'}
 */
function syncFile(src, dest) {
  if (!existsSync(src)) return 'missing-asset'
  const next = readFileSync(src)
  if (existsSync(dest)) {
    const prev = readFileSync(dest)
    if (prev.length === next.length && prev.equals(next)) return 'unchanged'
  }
  mkdirSync(dirname(dest), { recursive: true })
  writeFileSync(dest, next)
  return 'written'
}

/**
 * Install the skill into the DSH home.
 * @returns {{ dir: string, written: string[], unchanged: string[], missing: string[] }}
 */
export function installSkill() {
  const dir = join(dshHome(), 'skills', 'codex-cua')
  const out = { dir, written: [], unchanged: [], missing: [] }
  for (const [rel, target] of FILES) {
    const result = syncFile(join(ASSETS, rel), join(dir, target))
    if (result === 'written') out.written.push(target)
    else if (result === 'unchanged') out.unchanged.push(target)
    else out.missing.push(rel)
  }
  return out
}

export function apply(ctx) {
  // Defer so profile composition finishes first.
  queueMicrotask(() => {
    try {
      const r = installSkill()
      const logger = ctx?.logger
      const msg =
        `dsh-codex-cua: skill at ${r.dir} ` +
        `(written: ${r.written.length}, unchanged: ${r.unchanged.length})` +
        (r.missing.length ? `; missing assets: ${r.missing.join(', ')}` : '')
      if (logger?.info) logger.info(msg)
      else if (process.env.DSH_CODEX_CUA_VERBOSE) console.log(msg)
      if (r.missing.length) {
        const warn = `dsh-codex-cua: package is incomplete, reinstall it. Missing: ${r.missing.join(', ')}`
        if (logger?.warn) logger.warn(warn)
        else console.warn(warn)
      }
    } catch (error) {
      const msg = `dsh-codex-cua: failed to install skill — ${String(error?.message || error)}`
      if (ctx?.logger?.warn) ctx.logger.warn(msg)
      else console.warn(msg)
    }
  })
}
