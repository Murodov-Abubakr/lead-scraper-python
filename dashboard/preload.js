const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('api', {
  getData:   ()             => ipcRenderer.invoke('get-data'),
  markLead:  (key, stage)  => ipcRenderer.invoke('mark-lead', { key, stage }),
  runScript: (script)      => ipcRenderer.invoke('run-script', script),
  stopScript:()            => ipcRenderer.invoke('stop-script'),
  onOutput:  (cb)          => ipcRenderer.on('script-output', (_, d) => cb(d)),
  onDone:    (cb)          => ipcRenderer.on('script-done',   (_, c) => cb(c)),
})
