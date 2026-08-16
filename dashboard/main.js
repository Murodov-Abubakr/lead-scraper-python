const { app, BrowserWindow, ipcMain } = require('electron')
const path = require('path')
const fs   = require('fs')
const { spawn } = require('child_process')

// Data files live one level up (Scrapper_and_etc/)
const DATA_DIR = path.join(__dirname, '..')

// ── CSV parser ──────────────────────────────────────────────────────

function parseCSV(text) {
  const lines = text.split('\n').filter(l => l.trim())
  if (!lines.length) return []

  function parseRow(line) {
    const fields = []
    let f = '', q = false
    for (const ch of line) {
      if (ch === '"')          q = !q
      else if (ch === ',' && !q) { fields.push(f); f = '' }
      else                     f += ch
    }
    fields.push(f)
    return fields.map(s => s.trim().replace(/^"|"$/g, ''))
  }

  const headers = parseRow(lines[0])
  return lines.slice(1).filter(l => l.trim()).map(line => {
    const vals = parseRow(line)
    return Object.fromEntries(headers.map((h, i) => [h, vals[i] ?? '']))
  })
}

// ── JSON helper ─────────────────────────────────────────────────────

function readJSON(file, fallback = {}) {
  try { return JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), 'utf8')) }
  catch { return fallback }
}

// ── IPC: load all data ──────────────────────────────────────────────

ipcMain.handle('get-data', () => {
  let leads = []
  try {
    leads = parseCSV(fs.readFileSync(path.join(DATA_DIR, 'dental_leads.csv'), 'utf8'))
  } catch (_) {}

  const sent    = readJSON('outreach_sent.json')
  const seq     = readJSON('email_sequence.json')
  const owner   = readJSON('owner_outreach.json')
  const replied = readJSON('replied.json')

  const enriched = leads.map(lead => {
    const key = (lead.business_name || '').toLowerCase().trim()
    const s   = sent[key]    || {}
    const sq  = seq[key]     || {}
    const o   = owner[key]   || {}
    const r   = replied[key] || {}

    let stage = 'scraped'
    if (r.stage)                          stage = r.stage
    else if (s.status === 'no_email')     stage = 'no_email'
    else if (s.status === 'failed')       stage = 'failed'
    else if (s.status === 'sent') {
      stage = (parseInt(sq.step) || 1) > 1 ? 'followed_up' : 'emailed'
    }

    return {
      ...lead,
      _key:           key,
      sent_status:    s.status  || 'pending',
      sent_email:     s.email   || lead.email || '',
      sent_at:        s.timestamp || '',
      sequence_step:  parseInt(sq.step) || 0,
      owner_name:     o.owner_name || '',
      owner_email:    o.email      || '',
      owner_status:   o.status     || '',
      pipeline_stage: stage,
    }
  })

  return { leads: enriched }
})

// ── IPC: manually mark a lead's stage ──────────────────────────────

ipcMain.handle('mark-lead', (_, { key, stage }) => {
  const p    = path.join(DATA_DIR, 'replied.json')
  const data = readJSON('replied.json')
  if (stage === 'remove') delete data[key]
  else data[key] = { stage, updated_at: new Date().toISOString() }
  fs.writeFileSync(p, JSON.stringify(data, null, 2))
  return true
})

// ── IPC: run Python script ──────────────────────────────────────────

let _proc = null

ipcMain.handle('run-script', (event, scriptName) => {
  if (_proc) { _proc.kill(); _proc = null }
  const scriptPath = path.join(DATA_DIR, scriptName)
  _proc = spawn('python', [scriptPath], { cwd: DATA_DIR })
  const send = text => { try { event.sender.send('script-output', text) } catch (_) {} }
  _proc.stdout.on('data', d => send(d.toString()))
  _proc.stderr.on('data', d => send(d.toString()))
  _proc.on('close', code => { event.sender.send('script-done', code); _proc = null })
  return true
})

ipcMain.handle('stop-script', () => { if (_proc) { _proc.kill(); _proc = null } })

// ── Window ──────────────────────────────────────────────────────────

function createWindow() {
  const win = new BrowserWindow({
    width: 1280, height: 800,
    minWidth: 960, minHeight: 640,
    backgroundColor: '#0c0e1a',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })
  win.loadFile('renderer.html')
}

app.whenReady().then(createWindow)
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
