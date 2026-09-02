/* Erinnerungen - Frontend.
   Der Server ist zustandslos gegenueber iCloud; hier wird optimistisch
   gerendert und bei Fehlern der Serverstand wieder eingeholt. */
'use strict';

/* Die Oberfläche spricht ausschließlich über HTTP mit der API und kennt sonst
   nichts von ihr. Wird sie getrennt ausgeliefert, zeigt REMINDERS_API_BASE auf
   die API (siehe web/public/config.js). */
const API = (window.REMINDERS_API_BASE || '').replace(/\/$/, '') + '/v1';

const $ = (s) => document.querySelector(s);
const el = (t, c, txt) => { const n = document.createElement(t); if (c) n.className = c; if (txt != null) n.textContent = txt; return n; };

const VIEWS = [
  { id: 'today',    icon: '📅', name: 'Heute' },
  { id: 'upcoming', icon: '🗓', name: 'Demnächst' },
  { id: 'flagged',  icon: '❗️', name: 'Priorität' },
  { id: 'nodate',   icon: '∅',  name: 'Ohne Datum' },
  { id: 'all',      icon: '≡',  name: 'Alle offenen' },
];

const state = {
  scope: { kind: 'view', id: 'today' },
  lists: [], tasks: [], counts: {}, today: null,
  q: '', showDone: false, selected: null, inflight: 0,
};

/* ---------------- HTTP ---------------- */

function setBusy(on, label) {
  state.inflight += on ? 1 : -1;
  if (state.inflight < 0) state.inflight = 0;
  const s = $('#status');
  $('#refreshBtn').classList.toggle('spin', state.inflight > 0);
  if (state.inflight > 0) { s.className = 'status busy'; s.textContent = label || 'iCloud…'; }
  else showSync();
}

async function api(path, opts = {}, label) {
  setBusy(true, label);
  try {
    const res = await fetch(API + path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* leere Antwort */ }
    if (!res.ok) {
      const msg = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.setup = !!(data && data.setup);
      throw err;
    }
    $('#status').className = 'status';
    return data;
  } finally { setBusy(false); }
}

function showSync() {
  const s = $('#status');
  if (state.inflight > 0) return;
  const sy = state.sync || {};
  if (sy.error) { s.className = 'status err'; s.textContent = 'Sync-Fehler'; return; }
  s.className = 'status';
  if (sy.age == null) { s.textContent = ''; return; }
  s.textContent = sy.age < 60 ? 'aktuell'
    : sy.age < 3600 ? `vor ${Math.round(sy.age / 60)} min geladen`
    : `vor ${Math.round(sy.age / 3600)} h geladen`;
}

let toastTimer;
function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast on ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, kind === 'err' ? 7000 : 3200);
}

function fail(err) {
  console.error(err);
  toast(err.message || 'Unbekannter Fehler', 'err');
  const s = $('#status'); s.className = 'status err'; s.textContent = 'Fehler';
  if (err.setup) showSetupBanner();
}

function showSetupBanner() { openLogin(); }

/* Anmeldung direkt auf der Seite. Das Passwort geht nur an den eigenen Server
   weiter (hinter tinyauth, per HTTPS) und wird dort nicht gespeichert. */
async function openLogin(reason) {
  const st = await fetch(`${API}/session`).then((r) => r.json()).catch(() => null);
  if (st && st.valid) { toast('Session ist gültig.', 'ok'); return; }

  const dlg = $('#login');
  $('#loginWhy').textContent = reason || (st && st.reason) || 'Die iCloud-Session ist abgelaufen.';
  $('#loginId').textContent = (st && st.apple_id) || '';
  $('#loginStep2').classList.add('hidden');
  $('#loginStep1').classList.remove('hidden');
  $('#loginPw').value = '';
  $('#loginCode').value = '';
  $('#loginErr').textContent = '';
  dlg.classList.add('on');
  $('#scrim').classList.add('on');
  setTimeout(() => $('#loginPw').focus(), 80);
}

function closeLogin() {
  $('#login').classList.remove('on');
  $('#scrim').classList.remove('on');
}

$('#loginClose').onclick = closeLogin;

$('#loginStep1').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('#loginGo');
  const pw = $('#loginPw').value;
  if (!pw) return;
  btn.disabled = true; $('#loginErr').textContent = '';
  try {
    const r = await api('/session/login', { method: 'POST', body: { password: pw } }, 'Anmelden…');
    $('#loginPw').value = '';
    if (r.needs_2fa) {
      $('#loginStep1').classList.add('hidden');
      $('#loginStep2').classList.remove('hidden');
      setTimeout(() => $('#loginCode').focus(), 60);
    } else {
      closeLogin(); toast('Angemeldet.', 'ok'); await load();
    }
  } catch (e) {
    $('#loginErr').textContent = e.message;
  } finally { btn.disabled = false; }
});

$('#loginStep2').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('#codeGo');
  const code = $('#loginCode').value.trim();
  if (!code) return;
  btn.disabled = true; $('#loginErr').textContent = '';
  try {
    await api('/session/code', { method: 'POST', body: { code } }, 'Bestätigen…');
    closeLogin(); toast('Angemeldet.', 'ok'); await load();
  } catch (e) {
    $('#loginErr').textContent = e.message;
  } finally { btn.disabled = false; }
});

/* ---------------- Datum ---------------- */

const WD = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];

function dueLabel(t) {
  if (!t.due) return null;
  const hasTime = t.due_has_time;
  const d = new Date(t.due + (hasTime ? '' : 'T12:00'));
  const time = hasTime ? d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }) : '';
  const days = t.days;
  let day;
  if (days === 0) day = 'Heute';
  else if (days === 1) day = 'Morgen';
  else if (days === -1) day = 'Gestern';
  else if (days > 1 && days < 7) day = WD[d.getDay()];
  else day = d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: days > 300 || days < -300 ? 'numeric' : undefined });
  return day + (time ? ` ${time}` : '');
}

function bucket(t) {
  if (t.completed) return { k: 9, name: 'Erledigt' };
  if (t.days == null) return { k: 8, name: 'Ohne Datum' };
  if (t.days < 0) return { k: 0, name: 'Überfällig', hot: true };
  if (t.days === 0) return { k: 1, name: 'Heute' };
  if (t.days === 1) return { k: 2, name: 'Morgen' };
  if (t.days < 7) return { k: 3, name: 'Diese Woche' };
  if (t.days < 31) return { k: 4, name: 'Diesen Monat' };
  return { k: 5, name: 'Später' };
}

/* ---------------- Laden + Rendern ---------------- */

async function load(opts = {}) {
  const p = new URLSearchParams();
  if (state.scope.kind === 'list') {
    p.set('list_id', state.scope.id);
    if (state.showDone) p.set('include_completed', 'true');
  } else {
    p.set('view', state.scope.id);
  }
  if (state.q) p.set('q', state.q);

  try {
    // Alles aus dem Cache der API — drei Aufrufe kosten zusammen Millisekunden.
    const [lists, tasks, counts, sync] = await Promise.all([
      api('/lists', {}, opts.label),
      api('/tasks?' + p.toString()),
      api('/views'),
      api('/sync'),
    ]);
    Object.assign(state, { lists, tasks, counts, sync: sync || {} });
    $('#banner').className = 'banner hidden';
    showSync();
    render();
  } catch (e) { fail(e); renderTasks(); }
}

function render() { renderSidebar(); renderHead(); renderListPicker(); renderTasks(); }

function renderSidebar() {
  const v = $('#views'); v.innerHTML = '';
  for (const view of VIEWS) {
    const b = el('button', 'navitem' + (state.scope.kind === 'view' && state.scope.id === view.id ? ' on' : ''));
    b.append(el('span', 'ic', view.icon), el('span', 'nm', view.name));
    const c = state.counts[view.id];
    if (c) b.append(el('span', 'ct', String(c)));
    b.onclick = () => go({ kind: 'view', id: view.id });
    v.append(b);
  }

  const l = $('#lists'); l.innerHTML = '';
  for (const list of state.lists) {
    const b = el('button', 'navitem' + (state.scope.kind === 'list' && state.scope.id === list.id ? ' on' : ''));
    const dot = el('span', 'dot');
    if (list.color) dot.style.background = list.color;
    b.append(dot, el('span', 'nm', list.name));
    if (list.open_count) b.append(el('span', 'ct', String(list.open_count)));
    b.onclick = () => go({ kind: 'list', id: list.id });

    b.addEventListener('dragover', (ev) => {
      if (!drag || drag.list_id === list.id || !drag.movable) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      b.classList.add('drop');
    });
    b.addEventListener('dragleave', () => b.classList.remove('drop'));
    b.addEventListener('drop', async (ev) => {
      ev.preventDefault();
      b.classList.remove('drop');
      const t = drag; drag = null;
      if (!t || t.list_id === list.id) return;
      if (!t.movable) {
        toast(`Nicht in eine andere Liste verschiebbar: ${t.block_reason} ginge verloren.`, 'err');
        return;
      }
      try {
        await api(`/tasks/${t.list_id}/${t.id}/move`,
          { method: 'POST', body: { target_list_id: list.id } }, 'Verschieben…');
        toast(`„${t.title}" → ${list.name}`, 'ok');
        await load();
      } catch (e) { fail(e); }
    });
    l.append(b);
  }
  if (!state.lists.length) l.append(el('div', 'side-foot', 'Keine Erinnerungslisten gefunden.'));
}

function currentList() { return state.lists.find((x) => x.id === state.scope.id) || null; }

function scopeName() {
  if (state.scope.kind === 'list') return currentList()?.name || 'Liste';
  return VIEWS.find((v) => v.id === state.scope.id)?.name || 'Erinnerungen';
}

function renderHead() {
  $('#paneTitle').textContent = scopeName();
  const open = state.tasks.filter((t) => !t.completed).length;
  const done = state.tasks.filter((t) => t.completed).length;
  let sub = open === 0 ? 'nichts offen' : `${open} offen`;
  if (done) sub += ` · ${done} erledigt`;
  if (state.q) sub += ` · Suche „${state.q}“`;
  $('#paneSub').textContent = sub;

  const a = $('#paneActions'); a.innerHTML = '';
  if (state.scope.kind === 'list') {
    const showBtn = el('button', 'btn ghost tiny', state.showDone ? 'Erledigte ausblenden' : 'Erledigte zeigen');
    showBtn.onclick = () => { state.showDone = !state.showDone; load(); };
    a.append(showBtn);
  }
}

function renderListPicker() {
  const sel = $('#qaList');
  const prev = sel.value;
  sel.innerHTML = '';
  for (const list of state.lists) {
    const o = el('option', null, list.name); o.value = list.id; sel.append(o);
  }
  const want = state.scope.kind === 'list' ? state.scope.id
    : (prev && state.lists.some((l) => l.id === prev) ? prev : (state.lists[0]?.id || ''));
  sel.value = want;
  $('#quickadd').classList.toggle('hidden', !state.lists.length);
}

function renderTasks() {
  const area = $('#taskarea'); area.innerHTML = '';
  if (!state.tasks.length) {
    const e = el('div', 'empty');
    e.append(el('div', 'big', state.q ? '🔍' : '✓'));
    e.append(el('div', null, state.q ? 'Nichts gefunden.'
      : (state.scope.kind === 'list' ? 'Diese Liste ist leer.' : 'Nichts zu tun. Schön.')));
    area.append(e);
    return;
  }

  // In einer Liste mit Trennern gruppieren wir wie iOS danach — sonst nach
  // Fälligkeit, weil Trenner über Listen hinweg keine Bedeutung haben.
  const list = currentList();
  const bySection = state.scope.kind === 'list' && list && list.sections && list.sections.length;

  const groups = new Map();
  for (const t of state.tasks) {
    const b = bySection
      ? (t.section_id
          ? { k: t.section_order, name: t.section_name, sec: true }
          : { k: 9998, name: 'Ohne Trenner', sec: true })
      : bucket(t);
    if (!groups.has(b.k)) groups.set(b.k, { ...b, items: [] });
    groups.get(b.k).items.push(t);
  }
  // Leere Trenner trotzdem zeigen — sonst weicht die Struktur von der am
  // iPhone ab und man sucht einen Trenner, der nur gerade nichts enthält.
  if (bySection) {
    list.sections.forEach((sec, i) => {
      if (!groups.has(i)) groups.set(i, { k: i, name: sec.name, sec: true, items: [] });
    });
  }

  for (const g of [...groups.values()].sort((a, b) => a.k - b.k)) {
    if (!g.items.length) {
      const head = el('div', 'group');
      const ht = el('div', 'group-title sec empty-sec', `${g.name} · leer`);
      if (bySection) sectionDropTarget(ht, g);
      head.append(ht);
      area.append(head);
      continue;
    }
    const wrap = el('div', 'group');
    const title = el('div', 'group-title' + (g.hot ? ' hot' : '') + (g.sec ? ' sec' : ''),
      `${g.name} · ${g.items.length}`);
    const box = el('div', 'tasks');
    for (const t of g.items) box.append(taskRow(t));
    if (bySection) { sectionDropTarget(title, g); sectionDropTarget(box, g); }
    wrap.append(title, box);
    area.append(wrap);
  }
}

function taskRow(t) {
  const row = el('div', 'task' + (t.completed ? ' done' : '') + (state.selected === rowKey(t) ? ' sel' : ''));
  row.dataset.key = rowKey(t);

  const c = el('div', 'circle' + (t.completed ? ' on' : '') + (t.priority === 1 ? ' p1' : t.priority === 5 ? ' p5' : ''));
  c.title = t.completed ? 'Wieder öffnen' : 'Erledigt';
  c.onclick = (ev) => { ev.stopPropagation(); toggleDone(t, row); };

  const main = el('div', 't-main');
  main.append(el('div', 't-title', t.title || '(ohne Titel)'));
  if (t.notes) main.append(el('div', 't-notes', t.notes));

  const meta = el('div', 't-meta');
  const dl = dueLabel(t);
  if (dl) meta.append(el('span', 'badge due' + (t.overdue ? ' overdue' : t.today ? ' today' : ''), dl));
  if (t.priority === 1) meta.append(el('span', 'badge p1', 'Hoch'));
  if (t.priority === 5) meta.append(el('span', 'badge p5', 'Mittel'));
  if (state.scope.kind !== 'list' && t.list_name) meta.append(el('span', 'badge list', t.list_name));
  if (state.scope.kind !== 'list' && t.section_name) meta.append(el('span', 'badge sec', t.section_name));
  if (t.flagged) meta.append(el('span', 'badge p5', '🚩'));
  if (t.recurring) meta.append(el('span', 'badge lock', '↻ wiederholt'));
  if (t.linked) meta.append(el('span', 'badge lock', '⇲ Unteraufgabe'));
  if (meta.children.length) main.append(meta);

  row.append(c, main);
  row.onclick = () => openDetail(t);

  // Ziehen auf eine Liste in der Seitenleiste verschiebt die Erinnerung.
  // Zwischen Trennern geht das bewusst nicht — ihre Zuordnung liegt in einem
  // Sammel-Blob der Liste, den Apples API nicht sicher beschreibbar macht.
  row.draggable = true;
  row.addEventListener('dragstart', (ev) => {
    drag = t;
    row.classList.add('dragging');
    ev.dataTransfer.effectAllowed = 'move';
    ev.dataTransfer.setData('text/plain', t.title);
  });
  row.addEventListener('dragend', () => {
    drag = null; row.classList.remove('dragging');
    document.querySelectorAll('.drop, .drop-sec').forEach(
      (n) => n.classList.remove('drop', 'drop-sec'));
  });
  return row;
}

const rowKey = (t) => `${t.list_id}/${t.id}`;
let drag = null;   // gerade gezogene Erinnerung

/* Ablage auf einem Trenner. Anders als beim Verschieben zwischen Listen ist das
   ein reiner Feld-Update — es geht nichts an der Erinnerung verloren, deshalb
   auch keine movable-Prüfung. */
function sectionDropTarget(node, group) {
  const secId = group.k >= 9998 ? null : (currentList().sections[group.k] || {}).id || null;
  node.addEventListener('dragover', (ev) => {
    if (!drag || drag.section_id === (secId || '')) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    node.classList.add('drop-sec');
  });
  node.addEventListener('dragleave', () => node.classList.remove('drop-sec'));
  node.addEventListener('drop', async (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    node.classList.remove('drop-sec');
    const t = drag; drag = null;
    if (!t || t.section_id === (secId || '')) return;
    try {
      await api(`/tasks/${t.list_id}/${t.id}/section`,
        { method: 'POST', body: { section_id: secId } }, 'Trenner ändern…');
      toast(`„${t.title}" → ${group.name}`, 'ok');
      await load();
    } catch (e) { fail(e); }
  });
}

/* ---------------- Aktionen ---------------- */

function go(scope) {
  state.scope = scope;
  state.showDone = false;
  state.selected = null;
  closeDetail();
  $('#sidebar').classList.remove('on');
  $('#scrim').classList.remove('on');
  load();
}

async function toggleDone(t, row) {
  row.classList.add('busy');
  try {
    await api(`/tasks/${t.list_id}/${t.id}`, { method: 'PATCH', body: { completed: !t.completed } });
    await load();
  } catch (e) { fail(e); row.classList.remove('busy'); }
}

/* Schnelleingabe: zeigt live, was der Parser aus der Zeile macht. */
let previewTimer;
$('#qaInput').addEventListener('input', (e) => {
  clearTimeout(previewTimer);
  const text = e.target.value;
  if (!text.trim()) { $('#qaHint').innerHTML = ''; return; }
  previewTimer = setTimeout(async () => {
    try {
      const p = await fetch(`${API}/parse?text=` + encodeURIComponent(text)).then((r) => r.json());
      const bits = [`<b>${escapeHtml(p.title)}</b>`];
      if (p.due) {
        const d = new Date(p.due.includes('T') ? p.due : p.due + 'T12:00');
        const s = d.toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit' })
          + (p.due.includes('T') ? ' ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }) : '');
        bits.push(`<span class="tag">${s}</span>`);
      }
      if (p.priority) bits.push(`<span class="tag">${{ 1: 'Hoch', 5: 'Mittel', 9: 'Niedrig' }[p.priority]}</span>`);
      $('#qaHint').innerHTML = bits.join(' ');
    } catch (_) { /* Vorschau ist Beiwerk */ }
  }, 180);
});

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

$('#quickadd').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const input = $('#qaInput');
  const text = input.value.trim();
  const listId = $('#qaList').value;
  if (!text || !listId) return;
  input.value = ''; $('#qaHint').innerHTML = '';
  try {
    await api('/tasks', { method: 'POST', body: { list_id: listId, text } }, 'Anlegen…');
    await load();
  } catch (e) { fail(e); input.value = text; }
});

/* ---------------- Detailpanel ---------------- */

function closeDetail() {
  $('#detail').classList.remove('on');
  $('#detail').setAttribute('aria-hidden', 'true');
  $('#scrim').classList.remove('on');
  state.selected = null;
  document.querySelectorAll('.task.sel').forEach((n) => n.classList.remove('sel'));
}

function openDetail(t) {
  state.selected = rowKey(t);
  document.querySelectorAll('.task').forEach((n) => n.classList.toggle('sel', n.dataset.key === state.selected));
  $('#detail').classList.add('on');
  $('#detail').setAttribute('aria-hidden', 'false');
  if (window.matchMedia('(max-width:820px)').matches) $('#scrim').classList.add('on');

  const body = $('#detailBody');
  body.innerHTML = '';
  const draft = { ...t, _prio: t.priority, _flag: t.flagged };

  const fTitle = field('Titel');
  const iTitle = el('input'); iTitle.value = t.title; fTitle.append(iTitle);

  const fDue = field('Fällig');
  const row = el('div', 'row');
  const iDate = el('input'); iDate.type = 'date';
  const iTime = el('input'); iTime.type = 'time'; iTime.style.maxWidth = '130px';
  if (t.due) { iDate.value = t.due.slice(0, 10); if (t.due_has_time) iTime.value = t.due.slice(11, 16); }
  row.append(iDate, iTime);
  const chips = el('div', 'chips');
  const mk = (label, days) => {
    const c = el('button', 'chip', label); c.type = 'button';
    c.onclick = () => {
      if (days === null) { iDate.value = ''; iTime.value = ''; }
      else { const d = new Date(); d.setDate(d.getDate() + days); iDate.value = d.toISOString().slice(0, 10); }
      save();
    };
    return c;
  };
  chips.append(mk('Heute', 0), mk('Morgen', 1), mk('In 1 Woche', 7), mk('Kein Datum', null));
  fDue.append(row, chips);

  const fPrio = field('Priorität');
  const seg = el('div', 'segbar');
  const opts = [[0, 'Keine', ''], [9, 'Niedrig', ''], [5, 'Mittel', 'p5'], [1, 'Hoch', 'p1']];
  const segBtns = [];
  for (const [val, label, cls] of opts) {
    const b = el('button', 'seg' + (t.priority === val ? ` on ${cls}` : '')); b.type = 'button';
    b.textContent = label; b.dataset.val = val;
    b.onclick = () => {
      draft.priority = val;
      segBtns.forEach((x) => { x.className = 'seg' + (+x.dataset.val === val ? ` on ${opts.find((o) => o[0] === val)[2]}` : ''); });
      save();
    };
    segBtns.push(b); seg.append(b);
  }
  fPrio.append(seg);

  const fNotes = field('Notizen');
  const iNotes = el('textarea'); iNotes.value = t.notes; fNotes.append(iNotes);

  const fFlag = field('Markierung');
  const flagBtn = el('button', 'seg' + (t.flagged ? ' on p5' : ''));
  flagBtn.type = 'button';
  flagBtn.textContent = t.flagged ? '🚩 Markiert' : 'Nicht markiert';
  flagBtn.onclick = () => {
    draft.flagged = !draft.flagged;
    flagBtn.className = 'seg' + (draft.flagged ? ' on p5' : '');
    flagBtn.textContent = draft.flagged ? '🚩 Markiert' : 'Nicht markiert';
    save(true);
  };
  fFlag.append(flagBtn);

  const fList = field('Liste');
  const sList = el('select');
  for (const l of state.lists) { const o = el('option', null, l.name); o.value = l.id; sList.append(o); }
  sList.value = t.list_id;
  fList.append(sList);
  if (!t.movable) {
    sList.disabled = true;
    const why = el('span', 'small muted',
      `Verschieben hier nicht möglich — ${t.block_reason} ginge dabei verloren. Am iPhone verschieben.`);
    fList.append(why);
  }

  body.append(fTitle, fDue, fPrio, fNotes, fFlag, fList);

  if (t.recurring || t.linked) {
    const warn = el('div', 'banner warn');
    warn.textContent = t.recurring
      ? 'Diese Erinnerung wiederholt sich. Die Wiederholung bleibt unangetastet — bearbeite sie am iPhone.'
      : 'Diese Erinnerung hängt an einer Unteraufgaben-Kette. Die Verknüpfung bleibt erhalten, außer beim Verschieben in eine andere Liste.';
    body.append(warn);
  }

  const foot = el('div', 'detail-foot');
  const savedMark = el('span', 'saved', '✓ gespeichert');
  const del = el('button', 'btn danger tiny', 'Löschen');
  del.onclick = async () => {
    if (!confirm(`„${t.title}“ endgültig aus iCloud löschen?`)) return;
    try {
      await api(`/tasks/${t.list_id}/${t.id}`, { method: 'DELETE' }, 'Löschen…');
      closeDetail(); toast('Gelöscht', 'ok'); await load();
    } catch (e) { fail(e); }
  };
  foot.append(savedMark, del);
  body.append(foot);

  let saveTimer, saving = false;
  function save(immediate) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(doSave, immediate ? 0 : 600);
  }

  async function doSave() {
    if (saving) return;
    const due = iDate.value ? (iTime.value ? `${iDate.value}T${iTime.value}` : iDate.value) : null;
    const patch = {};
    if (iTitle.value.trim() && iTitle.value !== draft.title) patch.title = iTitle.value.trim();
    if (iNotes.value !== draft.notes) patch.notes = iNotes.value;
    if (draft.flagged !== draft._flag) patch.flagged = draft.flagged;
    if (draft.priority !== draft._prio) patch.priority = draft.priority;
    const curDue = draft.due || null;
    if (due !== curDue) patch.due = due;
    if (!Object.keys(patch).length) return;

    saving = true;
    try {
      const updated = await api(`/tasks/${t.list_id}/${t.id}`, { method: 'PATCH', body: patch }, 'Speichern…');
      Object.assign(draft, updated, { _prio: updated.priority, _flag: updated.flagged });
      savedMark.classList.add('on');
      setTimeout(() => savedMark.classList.remove('on'), 1400);
      await load();
    } catch (e) { fail(e); } finally { saving = false; }
  }

  iTitle.oninput = () => save();
  iNotes.oninput = () => save();
  iDate.onchange = () => save(true);
  iTime.onchange = () => save(true);

  sList.onchange = async () => {
    const target = sList.value;
    if (target === t.list_id) return;
    try {
      await api(`/tasks/${t.list_id}/${t.id}/move`,
        { method: 'POST', body: { target_list_id: target } }, 'Verschieben…');
      toast(`Verschoben nach „${state.lists.find((l) => l.id === target)?.name}“`, 'ok');
      closeDetail(); await load();
    } catch (e) { fail(e); sList.value = t.list_id; }
  };

  setTimeout(() => iTitle.focus(), 60);
}

function field(label) {
  const f = el('div', 'field');
  f.append(el('span', null, label));
  return f;
}

/* ---------------- Listenverwaltung ---------------- */

/* Listen anlegen/umbenennen/löschen bietet Apples CloudKit-API nicht an —
   das passiert am iPhone. Hier sind Listen nur lesbar. */
$('#newListBtn').onclick = () => toast(
  'Neue Listen legst du am iPhone an — Apples CloudKit-API kann das nicht. '
  + 'Sie erscheinen hier nach dem nächsten Laden.', 'err');

/* ---------------- Suche, Tasten, Start ---------------- */

let searchTimer;
$('#search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); load(); }, 260);
});

$('#refreshBtn').onclick = async () => {
  try { await api('/sync', { method: 'POST' }, 'Neu laden…'); await load(); }
  catch (e) { fail(e); }
};
$('#detailClose').onclick = closeDetail;
$('#scrim').onclick = () => {
  if ($('#login').classList.contains('on')) return;  // Anmeldung nicht wegklicken
  closeDetail(); $('#sidebar').classList.remove('on'); $('#scrim').classList.remove('on');
};
$('#menuBtn').onclick = () => {
  const open = $('#sidebar').classList.toggle('on');
  $('#scrim').classList.toggle('on', open);
};

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { if ($('#login').classList.contains('on')) closeLogin(); else closeDetail(); return; }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (typing) return;
  if (e.key === 'n') { e.preventDefault(); $('#qaInput').focus(); }
  else if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
  else if (e.key === 'r') { e.preventDefault(); $('#refreshBtn').click(); }
});

/* Beim Zurueckkehren auf den Tab still nachladen - im Buero laeuft das
   Fenster oft stundenlang, waehrend am iPhone weitergearbeitet wird. */
let lastSyncSeen = 0;
setInterval(async () => {
  if (document.hidden || state.inflight > 0) return;
  try {
    const sy = await fetch(`${API}/sync`).then((r) => r.json());
    state.sync = sy; showSync();
    if (sy.last_sync && sy.last_sync !== lastSyncSeen) {
      lastSyncSeen = sy.last_sync;
      await load();                 // kommt aus dem Cache, kostet Millisekunden
    }
  } catch (_) { /* Anzeige ist Beiwerk */ }
}, 15000);

load({ label: 'Laden…' }).then(() => { lastSyncSeen = (state.sync || {}).last_sync || 0; });
