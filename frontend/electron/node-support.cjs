const { engines } = require('../package.json')

// Keep the policy in package.json so npm, setup and the launcher agree.
// Supported release lines are explicit caret ranges; prereleases are excluded.
function isSupportedNodeVersion(version) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(version.trim())
  if (!match) return false
  const [, major, minor, patch] = match.map(Number)
  return engines.node.split(' || ').some((range) => {
    const minimum = /^\^(\d+)\.(\d+)\.(\d+)$/.exec(range)
    if (!minimum) throw new Error(`Unsupported Node policy format: ${range}`)
    const [, requiredMajor, requiredMinor, requiredPatch] = minimum.map(Number)
    return major === requiredMajor && (
      minor > requiredMinor || (minor === requiredMinor && patch >= requiredPatch)
    )
  })
}

if (require.main === module) {
  const version = process.versions.node
  const supported = isSupportedNodeVersion(version)
  process.stdout.write(JSON.stringify({ version, supported, required: engines.node }))
  process.exitCode = supported ? 0 : 1
}

module.exports = { isSupportedNodeVersion }
