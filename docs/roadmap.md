# Roadmap

Planned improvements that are not built yet.

## Browser control fully on your own Chrome profile

Browser control now always uses your own Chrome profile; Monaw no longer has a separate isolated profile. It is not seamless yet:

- Chrome 144 and later asks **Allow remote debugging?** on connect, and may ask again after a reconnection.
- If Chrome was already running before its first setup, you have to close it once so Monaw can prepare the profile.
- Settings meant for an isolated browser, such as hiding the browser window, have little effect on your visible Chrome.

The goal is for Monaw to control the Chrome you already have open, with your logins and tabs, without restarts or repeated prompts. See [browser-automation.md](browser-automation.md) for how it works today.
