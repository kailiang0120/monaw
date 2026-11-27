const fs = require('node:fs')

// Verify that Electron's postinstall downloaded the desktop executable.
function checkElectronInstall() {
  try {
    const executable = require('electron')
    if (!fs.statSync(executable).isFile()) {
      throw new Error('The Electron executable is missing.')
    }
    console.log(`Electron is installed: ${executable}`)
  } catch (error) {
    console.error(`Electron installation is incomplete: ${error.message}`)
    console.error('Allow the locked Electron version in package.json allowScripts, then run setup.bat again. Check that npm ignore-scripts and ELECTRON_SKIP_BINARY_DOWNLOAD are not enabled.')
    process.exitCode = 1
  }
}

checkElectronInstall()
