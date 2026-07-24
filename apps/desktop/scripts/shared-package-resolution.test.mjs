import { createRequire } from 'node:module'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

const DESKTOP_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(DESKTOP_ROOT, '..', '..')
const SHARED_ROOT = path.join(REPO_ROOT, 'apps', 'shared')
const temporaryRoots = []
const exportedSubpaths = ['', '/billing', '/billing-policy', '/charge-settlement', '/skin']

function readJson(filePath) {
  return JSON.parse(readFileSync(filePath, 'utf-8'))
}

function createAliasResolutionFixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hades-shared-aliases-'))
  temporaryRoots.push(root)
  const nodeModules = path.join(root, 'node_modules')

  for (const scope of ['@hades', '@hermes']) {
    const scopeRoot = path.join(nodeModules, scope)
    mkdirSync(scopeRoot, { recursive: true })
    symlinkSync(SHARED_ROOT, path.join(scopeRoot, 'shared'), process.platform === 'win32' ? 'junction' : 'dir')
  }

  const entry = path.join(root, 'probe.cjs')
  writeFileSync(entry, '')
  return createRequire(entry)
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true })
  }
})

describe('shared workspace package resolution', () => {
  it('locks both package names to the canonical shared workspace', () => {
    const desktopPackage = readJson(path.join(DESKTOP_ROOT, 'package.json'))
    const tuiPackage = readJson(path.join(REPO_ROOT, 'ui-tui', 'package.json'))
    const sharedPackage = readJson(path.join(SHARED_ROOT, 'package.json'))
    const lock = readJson(path.join(REPO_ROOT, 'package-lock.json'))

    expect(sharedPackage.name).toBe('@hades/shared')
    expect(desktopPackage.dependencies['@hades/shared']).toBe('file:../shared')
    expect(desktopPackage.dependencies['@hermes/shared']).toBe('file:../shared')
    expect(tuiPackage.dependencies['@hades/shared']).toBe('file:../apps/shared')
    expect(tuiPackage.dependencies['@hermes/shared']).toBe('file:../apps/shared')
    expect(lock.packages['node_modules/@hades/shared']).toEqual({
      resolved: 'apps/shared',
      link: true
    })
    expect(lock.packages['node_modules/@hermes/shared']).toEqual({
      resolved: 'apps/shared',
      link: true
    })
  })

  it.each(exportedSubpaths)('resolves both names through the same export target: %s', subpath => {
    const require = createAliasResolutionFixture()

    expect(require.resolve(`@hermes/shared${subpath}`)).toBe(require.resolve(`@hades/shared${subpath}`))
  })
})
