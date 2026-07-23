import { readFileSync } from 'node:fs'

import * as canonicalShared from '@hades/shared'
import * as legacyShared from '@hermes/shared'
import { describe, expect, it } from 'vitest'

describe('shared workspace package identity', () => {
  it('is Hades-canonical while both package aliases load one implementation', () => {
    const metadata = JSON.parse(readFileSync(new URL('../../../shared/package.json', import.meta.url), 'utf-8')) as {
      name?: string
    }

    expect(metadata.name).toBe('@hades/shared')
    expect(legacyShared.JsonRpcGatewayClient).toBe(canonicalShared.JsonRpcGatewayClient)
    expect(legacyShared.resolveGatewayWsUrl).toBe(canonicalShared.resolveGatewayWsUrl)
  })
})
