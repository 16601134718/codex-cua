#!/usr/bin/env node
/**
 * Verify the plugin without installing it into DSH.
 *
 * Checks that the manifest is installable (`dsh.bundle` present), that every
 * asset exists, and that installSkill() produces the expected skill directory
 * under a throwaway DSH_HOME.
 *
 *   node scripts/verify.mjs
 */
import { mkdtempSync, rmSync, existsSync, readFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
let failed = 0
const ok = (label, cond, detail = '') => {
  console.log(`[${cond ? 'OK  ' : 'FAIL'}] ${label}${detail ? '  — ' + detail : ''}`)
  if (!cond) failed++
}

console.log('dsh-codex-cua 自检\n' + '-'.repeat(48))

// 1. manifest
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'))
ok('package.json name = dsh-codex-cua', pkg.name === 'dsh-codex-cua', pkg.name)
ok('声明 dsh.bundle.patch', !!pkg.dsh?.bundle?.patch, pkg.dsh?.bundle?.patch ?? '(缺失)')
ok('声明 keywords dsh-plugin', (pkg.keywords ?? []).includes('dsh-plugin'))
ok('patch 文件存在', existsSync(join(ROOT, pkg.dsh?.bundle?.patch ?? 'x')))

// 2. assets
for (const rel of ['SKILL.md', 'cuad.py', 'docs/pitfalls.md', 'docs/protocol.md']) {
  const p = join(ROOT, 'assets', rel)
  ok(`asset ${rel}`, existsSync(p), existsSync(p) ? `${statSync(p).size} B` : '(缺失)')
}

// 3. SKILL.md 有合法 frontmatter
const skill = readFileSync(join(ROOT, 'assets', 'SKILL.md'), 'utf8')
const fm = skill.match(/^---\n([\s\S]*?)\n---/)
ok('SKILL.md 有 frontmatter', !!fm)
ok('SKILL.md 有单行 description', !!fm && /^description:\s*\S/m.test(fm[1]))

// 4. 真跑一次安装到临时 DSH_HOME
const home = mkdtempSync(join(tmpdir(), 'dsh-cua-'))
process.env.DSH_HOME = home
try {
  const { installSkill } = await import(join(ROOT, 'lib', 'index.js'))
  const r = installSkill()
  const dir = join(home, 'skills', 'codex-cua')
  ok('安装目录正确', r.dir === dir, r.dir)
  for (const f of ['SKILL.md', 'cuad.py', 'docs/pitfalls.md', 'docs/protocol.md']) {
    ok(`已安装 ${f}`, existsSync(join(dir, f)))
  }
  const again = installSkill()
  ok('重复安装幂等（第二次全部 unchanged）', again.written.length === 0, `written=${again.written.length}`)
  ok('无缺失资产', r.missing.length === 0, r.missing.join(',') || '(无)')
} catch (error) {
  ok('installSkill 执行', false, String(error?.message || error))
} finally {
  rmSync(home, { recursive: true, force: true })
}

// 5. cuad.py 语法（有 python3 才查）
try {
  const { execFileSync } = await import('node:child_process')
  execFileSync('python3', ['-c',
    `import ast,sys;ast.parse(open(${JSON.stringify(join(ROOT, 'assets', 'cuad.py'))},encoding='utf-8').read())`],
    { stdio: 'pipe' })
  ok('assets/cuad.py 语法正确', true)
} catch {
  ok('assets/cuad.py 语法正确', false, 'python3 不可用或语法错误')
}

console.log('-'.repeat(48))
console.log(failed === 0 ? '结论: 全部通过。' : `结论: ${failed} 项未通过。`)
process.exit(failed === 0 ? 0 : 1)
