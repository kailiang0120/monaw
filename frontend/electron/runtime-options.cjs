function backendPort(config, env = process.env) {
  const value = env.MONAW_BACKEND_PORT ?? config.backendPort ?? 8420
  if (!/^\d+$/.test(String(value))) throw new Error('Invalid Monaw backend port')
  const port = Number(value)
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid Monaw backend port')
  return port
}

function useDevelopmentRenderer(isPackaged, env = process.env) {
  const mode = env.MONAW_RENDERER_MODE || ''
  if (!['', 'dev', 'bundle'].includes(mode)) throw new Error('Invalid Monaw renderer mode')
  return !isPackaged && mode !== 'bundle'
}

module.exports = { backendPort, useDevelopmentRenderer }
