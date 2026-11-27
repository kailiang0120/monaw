import { useState } from 'react'
import { Plug } from 'lucide-react'

// Vite fingerprints these bundled assets; opening settings makes no CDN requests.
const logos = import.meta.glob<string>('../assets/connectors/*.svg', {
  eager: true,
  query: '?url',
  import: 'default',
})

/** Render a local brand asset with a stable, theme-aware fallback. */
export function ConnectorIcon({ providerId }: { providerId: string }) {
  const source = logos[`../assets/connectors/${providerId}.svg`]
  const [failedSource, setFailedSource] = useState<string | null>(null)
  if (!source || source === failedSource) return <Plug size={22} aria-hidden="true" />
  return (
    <img
      src={source}
      alt=""
      aria-hidden="true"
      width={24}
      height={24}
      decoding="async"
      loading="lazy"
      className="st-connector-logo"
      onError={() => setFailedSource(source)}
    />
  )
}
