// Diagnostics remain text, never markup, in this sandboxed local-only window.
const parameters = new URLSearchParams(window.location.search)
document.documentElement.dataset.theme = parameters.get('theme') === 'light' ? 'light' : 'dark'
document.title = parameters.get('title') || 'Monaw could not start'
document.getElementById('message-title').textContent = document.title
document.getElementById('message-body').textContent = parameters.get('message') || ''
document.getElementById('message-detail').textContent = parameters.get('detail') || ''
document.getElementById('quit').addEventListener('click', () => window.close())
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') window.close()
})
