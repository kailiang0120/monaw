import { describe, expect, it } from 'vitest'

import { formatAgentResponse } from './formatAgentResponse'

describe('formatAgentResponse', () => {
  it('preserves markdown content that is already structured', () => {
    const markdown = '## Summary\n- First item\n- Second item'

    expect(formatAgentResponse(markdown)).toBe(markdown)
  })

  it('formats structured plain text into markdown-friendly sections', () => {
    const content = `Here's a recent summary of the US stock market:

📊 Major Indices (Most Recent Close)
Index        Level        Change
S&P 500      7,173.91     +8.83 (+0.12%)
Nasdaq       24,887.10    +50.50 (+0.20%)

• What's Happening
Markets at All-Time Highs — The S&P 500 and Nasdaq both hit record levels.
Holiday / Santa Claus Rally Watch — This week has half-day trading on Wednesday.`

    expect(formatAgentResponse(content)).toContain('### 📊 Major Indices (Most Recent Close)')
    expect(formatAgentResponse(content)).toContain('| Index | Level | Change |')
    expect(formatAgentResponse(content)).toContain('- **Markets at All-Time Highs** — The S&P 500 and Nasdaq both hit record levels.')
    expect(formatAgentResponse(content)).toContain('### What\'s Happening')
  })
})
