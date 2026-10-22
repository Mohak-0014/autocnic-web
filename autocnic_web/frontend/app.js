// Shared helpers for every AutoCNIC page.
const LS_KEY = 'autocnic.session';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function fmt(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

function repoName(url) {
  const m = String(url || '').replace(/\.git$/, '').replace(/\/+$/, '').split(/[\\/]/);
  return m[m.length - 1] || url;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error((body && (body.detail || JSON.stringify(body))) || res.statusText);
    err.status = res.status;
    throw err;
  }
  return body;
}

function post(path, data) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
}

function toast(msg, isErr) {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  t.style.display = 'block';
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.style.display = 'none'; }, 6000);
}

// ---- session selection ------------------------------------------------
function currentSessionId() {
  const q = new URLSearchParams(location.search).get('s');
  if (q) { try { localStorage.setItem(LS_KEY, q); } catch (e) {} return q; }
  try { return localStorage.getItem(LS_KEY); } catch (e) { return null; }
}

function setSessionId(id) {
  try { localStorage.setItem(LS_KEY, id); } catch (e) {}
}

function pageLink(path, extra) {
  const sid = currentSessionId();
  const p = new URLSearchParams(extra || {});
  if (sid) p.set('s', sid);
  const qs = p.toString();
  return path + (qs ? '?' + qs : '');
}

// Renders the nav, loads the session, shows the read-only banner when the
// server no longer holds it in memory. Returns the session payload or null.
async function boot(active, opts) {
  opts = opts || {};
  const sid = currentSessionId();
  const links = [
    ['home', '/', 'Analyze', false],
    ['inputs', '/inputs', 'Inputs', true],
    ['track', '/track', 'Live tracker', true],
    ['certs', '/certificates', 'Certificates', true],
  ];
  let session = null;
  if (sid) {
    try { session = await api('/api/session/' + sid); } catch (e) { session = null; }
  }
  const nav = document.getElementById('nav');
  nav.className = 'nav';
  const LOGO = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="6" r="2.2"/><circle cx="5" cy="18" r="2.2"/><circle cx="19" cy="12" r="2.2"/><path d="M7 6.8l10 4.4M7 17.2l10-4.4"/></svg>`;
  nav.innerHTML = `
    <a class="brand" href="/"><span class="logo">${LOGO}</span>AutoCNIC</a>
    <div class="nav-links">
      ${links.map(([key, href, label, needs], i) =>
        `<a href="${needs ? pageLink(href) : href}" class="${key === active ? 'active' : ''} ${needs && !session ? 'disabled' : ''}"><span class="n">${i + 1}</span><span class="t">${label}</span></a>`).join('')}
    </div>
    <div class="nav-spacer"></div>
    ${session ? `<div class="session-chip" title="${esc(session.repo_url)}"><span class="led ${session.live ? '' : 'off'}"></span><b>${esc(repoName(session.repo_url))}</b><span class="mono">${esc(session.session_id)}</span></div>` : ''}`;

  if (!document.getElementById('footer')) {
    const f = document.createElement('footer');
    f.id = 'footer'; f.className = 'footer';
    f.innerHTML = `<div class="inner"><a class="brand" href="/"><span class="logo">${LOGO}</span>AutoCNIC</a>
      <span>Conditional dependency discovery with verifiable update certificates.</span><div class="spacer"></div>
      <a href="/">Analyze</a><a href="${session ? pageLink('/track') : '/'}">Live tracker</a><a href="${session ? pageLink('/certificates') : '/'}">Certificates</a></div>`;
    document.body.appendChild(f);
  }

  if (session && !session.live) {
    const b = document.createElement('div');
    b.className = 'banner';
    b.innerHTML = `<span>This session was recorded before the server restarted, so it is <b>read-only</b>. You can browse its graph, timeline and certificates.</span>
      <button class="small" id="reanalyze">Re-analyze this repo to make changes</button>`;
    nav.after(b);
    b.querySelector('#reanalyze').onclick = async (ev) => {
      ev.target.disabled = true; ev.target.textContent = 'Analyzing…';
      try {
        const d = await post('/api/analyze', {repo_url: session.repo_url});
        setSessionId(d.session_id);
        const dest = location.pathname.startsWith('/inputs') ? '/inputs' : '/track';
        location.href = dest + '?s=' + d.session_id;
      } catch (e) { toast('re-analyze failed: ' + e.message, true); ev.target.disabled = false; }
    };
  }
  if (!session && opts.needSession) {
    const main = document.querySelector('[data-main]') || document.body;
    main.innerHTML = `<div class="page"><div class="empty"><h3>No session selected</h3>
      <p>${sid ? 'That session was not found.' : 'Analyze a repository first.'}</p>
      <a class="btn" href="/">Go to Analyze</a></div></div>`;
    return null;
  }
  return session;
}

// ---- certificate wording ---------------------------------------------
function certClassLabel(cls) { return cls === 'VACUOUS' ? 'Vacuous' : 'Conditional'; }

function certStatement(c) {
  const A = (c.A || []).join(', ');
  if (c.cls === 'VACUOUS') {
    return `No dependency path leads from the changed input <b>${esc(c.input_node)}</b> to <b>${esc(c.target)}</b>, so its value <b>${esc(fmt(c.retained_value))}</b> was retained without recomputation.`;
  }
  return `A path from <b>${esc(c.input_node)}</b> to <b>${esc(c.target)}</b> exists, but every edge leaving the influenced set {${esc(A)}} was inactive before and after the change, so <b>${esc(fmt(c.retained_value))}</b> is provably unchanged.`;
}

function edgeLookup(graph) {
  const m = {};
  (graph.edges || []).forEach(e => { m[e.src + '->' + e.dst] = e; });
  return m;
}
