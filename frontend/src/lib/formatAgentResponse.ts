const MARKDOWN_SIGNAL_RE =
  /(^|\n)(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|~~~|\|.+\||!\[.*\]\(|\[.+\]\(.+\))/m

const BULLET_RE = /^[•●◦▪▫■□]\s+(.+)$/
const DASH_SECTION_RE = /^([^:]{2,72}?)\s+[—-]\s+(.+)$/
const COL_SPLIT_RE = /\s{2,}/

export function formatAgentResponse(content: string): string {
  const normalized = content.replace(/\r\n?/g, '\n').trim()
  if (!normalized) return ''
  if (MARKDOWN_SIGNAL_RE.test(normalized)) return normalized

  const lines = normalized.split('\n')
  const formatted: string[] = []

  for (let index = 0; index < lines.length; index += 1) {
    const current = lines[index]
    const trimmed = current.trim()

    if (!trimmed) {
      if (lastItem(formatted) !== '') {
        formatted.push('')
      }
      continue
    }

    const tableBlock = extractTableBlock(lines, index)
    if (tableBlock) {
      if (formatted.length > 0 && lastItem(formatted) !== '') {
        formatted.push('')
      }
      formatted.push(...tableBlock.markdown)
      formatted.push('')
      index = tableBlock.nextIndex - 1
      continue
    }

    if (isLikelyHeading(trimmed, findNextNonEmptyLine(lines, index + 1))) {
      if (formatted.length > 0 && lastItem(formatted) !== '') {
        formatted.push('')
      }
      formatted.push(`### ${normalizeHeading(trimmed)}`)
      continue
    }

    const bulletMatch = trimmed.match(BULLET_RE)
    if (bulletMatch) {
      formatted.push(`- ${bulletMatch[1]}`)
      continue
    }

    const dashSectionMatch = trimmed.match(DASH_SECTION_RE)
    if (dashSectionMatch && isLikelySectionLabel(dashSectionMatch[1])) {
      formatted.push(`- **${dashSectionMatch[1].trim()}** — ${dashSectionMatch[2].trim()}`)
      continue
    }

    formatted.push(trimmed)
  }

  return formatted.join('\n').replace(/\n{3,}/g, '\n\n')
}

function isLikelyHeading(line: string, nextLine?: string): boolean {
  if (!nextLine?.trim()) return false
  if (line.length > 72) return false
  if (/[.?!]$/.test(line)) return false
  if (COL_SPLIT_RE.test(line)) return false

  if (/:$/.test(line)) {
    const wordCount = normalizeHeading(line).split(/\s+/).filter(Boolean).length
    return wordCount <= 6
  }

  const normalized = normalizeHeadingForDetection(line)
  return /^[A-Z][A-Za-z0-9/&+(),' -]+$/.test(normalized)
}

function isLikelySectionLabel(value: string): boolean {
  const label = value.trim()
  return label.length >= 3 && label.length <= 48 && /^[A-Z0-9]/.test(label)
}

function extractTableBlock(lines: string[], startIndex: number): { markdown: string[]; nextIndex: number } | null {
  const rows: string[][] = []
  let index = startIndex

  while (index < lines.length) {
    const trimmed = lines[index].trim()
    if (!trimmed) break

    const cells = trimmed.split(COL_SPLIT_RE).map((cell) => cell.trim()).filter(Boolean)
    if (cells.length < 2 || cells.length > 4) break
    if (cells.some((cell) => cell.length > 80)) break
    if (/[—]/.test(trimmed)) break

    rows.push(cells)
    index += 1
  }

  if (rows.length < 2) return null

  const columnCount = rows[0].length
  if (!rows.every((row) => row.length === columnCount)) return null

  const markdown = [
    `| ${rows[0].join(' | ')} |`,
    `| ${rows[0].map(() => '---').join(' | ')} |`,
    ...rows.slice(1).map((row) => `| ${row.join(' | ')} |`),
  ]

  return { markdown, nextIndex: index }
}

function normalizeHeading(line: string): string {
  return line
    .replace(/^[•●◦▪▫■□]+\s+/, '')
    .replace(/:$/, '')
    .trim()
}

function normalizeHeadingForDetection(line: string): string {
  return normalizeHeading(line)
    .replace(/^[^\p{L}\p{N}]+/u, '')
    .trim()
}

function findNextNonEmptyLine(lines: string[], startIndex: number): string | undefined {
  for (let index = startIndex; index < lines.length; index += 1) {
    if (lines[index].trim()) {
      return lines[index].trim()
    }
  }

  return undefined
}

function lastItem<T>(items: T[]): T | undefined {
  return items.length > 0 ? items[items.length - 1] : undefined
}
