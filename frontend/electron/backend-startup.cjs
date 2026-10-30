const net = require('net')

/** True when something else already listens on the port Monaw's backend needs. */
function portInUse(host, port, createServer = net.createServer) {
  return new Promise((resolve) => {
    const server = createServer()
    server.once('error', (error) => resolve(error.code === 'EADDRINUSE' || error.code === 'EACCES'))
    server.once('listening', () => server.close(() => resolve(false)))
    server.listen(port, host)
  })
}

/** What to tell the user when the backend cannot start; never suggests stopping another program for them. */
function startupFailureMessage(reason, { port, logPath }) {
  if (reason.kind === 'port_in_use') {
    return {
      title: 'Monaw cannot start',
      message: `Port ${port} is already in use`,
      detail: `Another program is using port ${port}, which Monaw needs for its local service. Monaw did not stop that program. Close it, then start Monaw again.`,
    }
  }
  return {
    title: 'Monaw cannot start',
    message: 'The Monaw service stopped while starting',
    detail: `It exited with code ${reason.code}. The log is at ${logPath}.`,
  }
}

module.exports = { portInUse, startupFailureMessage }
