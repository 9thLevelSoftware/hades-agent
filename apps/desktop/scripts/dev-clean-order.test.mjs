import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

const DESKTOP_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(DESKTOP_ROOT, '..', '..')
const TOOL_NODE_MODULES = process.env.HADES_TEST_NODE_MODULES ?? path.join(REPO_ROOT, 'node_modules')
const ROOT_NODE_BIN = path.join(TOOL_NODE_MODULES, '.bin')
const NPM_CLI = process.env.npm_execpath
const TSC = path.join(TOOL_NODE_MODULES, 'typescript', 'bin', 'tsc')
const temporaryRoots = []

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: 'utf-8',
    env: {
      ...process.env,
      PATH: `${ROOT_NODE_BIN}${path.delimiter}${process.env.PATH ?? ''}`
    },
    timeout: 60_000
  })

  return result
}

function expectSuccess(result, label) {
  expect(result.status, `${label} failed\nstdout:\n${result.stdout ?? ''}\nstderr:\n${result.stderr ?? ''}`).toBe(0)
}

function emittedJavaScript(root, subtree) {
  const directory = path.join(root, subtree)
  if (!existsSync(directory)) return []

  return readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter(entry => entry.isFile() && entry.name === 'stale.js')
    .map(entry => path.join(entry.parentPath, entry.name))
}

function createFixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hades-desktop-clean-'))
  temporaryRoots.push(root)

  const desktopPackage = JSON.parse(readFileSync(path.join(DESKTOP_ROOT, 'package.json'), 'utf-8'))
  const desktopTsconfig = JSON.parse(readFileSync(path.join(DESKTOP_ROOT, 'tsconfig.json'), 'utf-8'))

  for (const directory of ['src', 'electron', 'e2e']) {
    mkdirSync(path.join(root, directory), { recursive: true })
    writeFileSync(path.join(root, directory, 'stale.ts'), `export const ${directory.replace('-', '')} = 1\n`)
  }

  writeFileSync(
    path.join(root, 'tsconfig.json'),
    JSON.stringify({
      compilerOptions: {
        target: 'ES2022',
        module: 'ESNext',
        moduleResolution: 'Bundler'
      },
      include: ['src'],
      references: desktopTsconfig.references
    })
  )
  writeFileSync(
    path.join(root, 'tsconfig.electron.json'),
    JSON.stringify({
      extends: './tsconfig.json',
      compilerOptions: {
        composite: true,
        declaration: true,
        outDir: 'build/electron-types'
      },
      include: ['electron'],
      exclude: ['src']
    })
  )
  writeFileSync(
    path.join(root, 'tsconfig.e2e.json'),
    JSON.stringify({
      extends: './tsconfig.json',
      compilerOptions: { composite: true },
      include: ['e2e'],
      exclude: ['src', 'electron']
    })
  )

  const scripts = desktopPackage.scripts
  writeFileSync(
    path.join(root, 'package.json'),
    JSON.stringify({
      private: true,
      scripts: {
        clean: scripts.clean,
        'clean:e2e': scripts['clean:e2e'],
        'clean:renderer': scripts['clean:renderer'],
        'clean:electron': scripts['clean:electron'],
        ...(scripts.predev ? { predev: scripts.predev } : {}),
        dev: 'node worker-check.mjs renderer && node worker-check.mjs electron'
      }
    })
  )

  for (const config of ['tsconfig.json', 'tsconfig.electron.json', 'tsconfig.e2e.json']) {
    expectSuccess(run(process.execPath, [TSC, '--build', config, '--force'], root), `priming ${config}`)
  }

  const stalePaths = [
    ...emittedJavaScript(root, 'src'),
    ...emittedJavaScript(root, 'e2e'),
    ...emittedJavaScript(root, 'build')
  ]
  expect(stalePaths.length).toBeGreaterThanOrEqual(3)

  writeFileSync(
    path.join(root, 'worker-check.mjs'),
    `import { appendFileSync, existsSync } from 'node:fs'\n` +
      `const stale = ${JSON.stringify(stalePaths)}\n` +
      `if (stale.some(existsSync)) process.exit(42)\n` +
      `appendFileSync('workers.log', process.argv[2] + '\\n')\n`
  )

  return { root, stalePaths }
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { force: true, recursive: true })
  }
})

describe('desktop stale TypeScript emit cleanup', () => {
  it('removes stale outputs before either full-dev worker starts', () => {
    const { root, stalePaths } = createFixture()

    expect(NPM_CLI).toBeTruthy()
    const result = run(process.execPath, [NPM_CLI, 'run', 'dev'], root)

    expectSuccess(result, 'npm run dev fixture')
    expect(stalePaths.every(stale => !existsSync(stale))).toBe(true)
    expect(readFileSync(path.join(root, 'workers.log'), 'utf-8')).toBe('renderer\nelectron\n')
  }, 30_000)

  it('renderer cleanup does not traverse and delete Electron build outputs', () => {
    const { root } = createFixture()
    const rendererOutputs = emittedJavaScript(root, 'src')
    const electronOutputs = emittedJavaScript(root, 'build')
    expect(rendererOutputs).not.toHaveLength(0)
    expect(electronOutputs).not.toHaveLength(0)

    expect(NPM_CLI).toBeTruthy()
    const result = run(process.execPath, [NPM_CLI, 'run', 'clean:renderer'], root)

    expectSuccess(result, 'standalone renderer clean')
    expect(rendererOutputs.every(output => !existsSync(output))).toBe(true)
    expect(electronOutputs.every(existsSync)).toBe(true)
  }, 30_000)
})
