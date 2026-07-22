import { spawn } from 'node:child_process'

export interface LaunchResult {
  code: null | number
  error?: string
}

/** Prefer Hades spellings; fall back to Hermes for dual-compat installs. */
const resolveHermesBin = () =>
  process.env.HADES_BIN?.trim() ||
  process.env.HERMES_BIN?.trim() ||
  'hades'

export const launchHermesCommand = (args: string[]): Promise<LaunchResult> =>
  new Promise(resolve => {
    const child = spawn(resolveHermesBin(), args, { stdio: 'inherit' })

    child.on('error', err => {
      // If `hades` is missing on PATH, retry once with the legacy `hermes` bin.
      if (!process.env.HADES_BIN?.trim() && !process.env.HERMES_BIN?.trim()) {
        const legacy = spawn('hermes', args, { stdio: 'inherit' })
        legacy.on('error', e2 => resolve({ code: null, error: e2.message }))
        legacy.on('exit', code => resolve({ code }))
        return
      }
      resolve({ code: null, error: err.message })
    })
    child.on('exit', code => resolve({ code }))
  })
