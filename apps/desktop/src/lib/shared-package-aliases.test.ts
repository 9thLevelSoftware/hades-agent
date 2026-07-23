// @vitest-environment node

import { readFileSync } from 'node:fs'

import * as canonicalShared from '@hades/shared'
import * as canonicalBillingModule from '@hades/shared/billing'
import type { UsageBarData as CanonicalUsageBarData } from '@hades/shared/billing'
import { BILLING_REFUSAL_POLICY as canonicalBillingPolicy } from '@hades/shared/billing-policy'
import { driveChargeSettlement as canonicalDriveChargeSettlement } from '@hades/shared/charge-settlement'
import { SKIN_COLOR_TOKENS as canonicalSkinColorTokens } from '@hades/shared/skin'
import * as legacyShared from '@hermes/shared'
import * as legacyBillingModule from '@hermes/shared/billing'
import type { UsageBarData as LegacyUsageBarData } from '@hermes/shared/billing'
import { BILLING_REFUSAL_POLICY as legacyBillingPolicy } from '@hermes/shared/billing-policy'
import { driveChargeSettlement as legacyDriveChargeSettlement } from '@hermes/shared/charge-settlement'
import { SKIN_COLOR_TOKENS as legacySkinColorTokens } from '@hermes/shared/skin'
import { describe, expect, it } from 'vitest'

describe('shared workspace package identity', () => {
  it('is Hades-canonical while Vite resolves every legacy subpath to one implementation', () => {
    const metadata = JSON.parse(readFileSync(new URL('../../../shared/package.json', import.meta.url), 'utf-8')) as {
      name?: string
    }
    const canonicalBilling: CanonicalUsageBarData = {
      kind: 'plan',
      remaining_display: '$0',
      total_display: '$0',
      spent_display: '$0',
      pct_used: 0,
      fill_fraction: 0
    }
    const legacyBilling: LegacyUsageBarData = canonicalBilling

    expect(metadata.name).toBe('@hades/shared')
    expect(legacyShared.JsonRpcGatewayClient).toBe(canonicalShared.JsonRpcGatewayClient)
    expect(legacyShared.resolveGatewayWsUrl).toBe(canonicalShared.resolveGatewayWsUrl)
    expect(legacyBilling).toBe(canonicalBilling)
    expect(Object.keys(legacyBillingModule)).toEqual(Object.keys(canonicalBillingModule))
    expect(legacyBillingPolicy).toBe(canonicalBillingPolicy)
    expect(legacyDriveChargeSettlement).toBe(canonicalDriveChargeSettlement)
    expect(legacySkinColorTokens).toBe(canonicalSkinColorTokens)
  })
})
