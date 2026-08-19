// Wired to the FastAPI backend — see backend/routers/*.py for endpoint shapes.
// No auth layer yet, so requests are scoped by a random per-browser X-User-Id
// (see getUserId()) stored in localStorage.

const API_BASE = "http://localhost:8000";

function getUserId() {
  let id = localStorage.getItem("nexchat_user_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("nexchat_user_id", id);
  }
  return id;
}
const userId = getUserId();

async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": userId,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) throw new Error(`${options.method || "GET"} ${path} failed: ${res.status}`);
  if (res.status === 204) return null;
  return res.json();
}

// Bypasses api() to read the X-Has-More response header (api() only ever returns the
// parsed body) — cursor-paginated, most recent page first unless `before` is given.
async function fetchMessages(sessionId, { before } = {}) {
  const params = new URLSearchParams({ limit: "50" });
  if (before) params.set("before", before);
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/messages?${params}`, {
    headers: { "X-User-Id": userId },
  });
  if (!res.ok) throw new Error(`GET messages failed: ${res.status}`);
  const messages = await res.json();
  return { messages, hasMore: res.headers.get("X-Has-More") === "true" };
}

// Parses the path-based routes this app serves: /chat/<id>, /chat/ (new chat), /memory/.
// `matched: false` means the path is something else (e.g. a bare "/") — callers use that to
// decide whether to leave the address bar alone on load rather than force a redirect.
function getRouteFromPath() {
  const path = location.pathname;
  const chatMatch = path.match(/^\/chat\/([^/]+)\/?$/);
  if (chatMatch) return { view: "chat", sessionId: decodeURIComponent(chatMatch[1]), matched: true };
  if (/^\/chat\/?$/.test(path)) return { view: "chat", sessionId: null, matched: true };
  if (/^\/memory\/?$/.test(path)) return { view: "memory", sessionId: null, matched: true };
  return { view: "chat", sessionId: null, matched: false };
}

function buildPath(view, sessionId) {
  return view === "memory" ? "/memory/" : sessionId ? `/chat/${sessionId}` : "/chat/";
}

// Reflects the open view/session in the URL path so routes are bookmarkable/shareable and
// back/forward navigation moves between them. pushState, not replaceState, since each
// navigation is a distinct point in the user's history — except on initial load, where the
// caller passes push:false to settle the address bar without adding a spurious entry.
function updateUrlForRoute(view, sessionId, { push = true } = {}) {
  const path = buildPath(view, sessionId);
  if (path === location.pathname) return;
  const state = { view, sessionId };
  if (push) history.pushState(state, "", path);
  else history.replaceState(state, "", path);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

marked.setOptions({ breaks: true, gfm: true });

// Assistant replies are markdown (code blocks, lists, bold, links) — rendering them as
// escaped plain text left literal backticks/asterisks on screen. marked's output still needs
// sanitizing before innerHTML: content originates from the model, which only echoes back
// what's in the conversation, but treating it as untrusted is the safe default regardless.
function renderMarkdown(content) {
  return DOMPurify.sanitize(marked.parse(content ?? ""));
}

const SUN_ICON_PATH = `<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>`;
const MOON_ICON_PATH = `<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/>`;

let sessions = [];          // SessionOut[] from GET /sessions
let sessionsLoadError = false;
let activeSessionId = null;
let activeMessages = [];    // {role, content, id?}[] for the open session — id is set for
                             // server-loaded messages, absent for ones just typed/streamed locally
let hasMoreMessages = false; // whether older messages exist for activeSessionId beyond what's loaded
let memorySessionId = null; // which session is selected in the Memory page's picker
let memoryData = null;      // MemoryDebugOut | null for memorySessionId
let searchQuery = "";
let openSessionMenuId = null; // which sidebar session's "..." menu is open, if any
let currentView = "chat";
let currentTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";

async function loadSessions() {
  try {
    // Server paginates (default page size 50); requesting a generous single page here
    // rather than building "load more sessions" UI — an unbounded *session count* is a much
    // lower near-term risk for a single local user than an unbounded *message count* within
    // one long-running conversation, which is what the messages endpoint's pagination below
    // actually protects against. Revisit if that assumption stops holding.
    sessions = await api("/sessions?limit=200");
    sessionsLoadError = false;
  } catch (e) {
    console.error("Failed to load sessions", e);
    sessions = [];
    sessionsLoadError = true;
  }
  renderSessionList();
}

async function renderDashboard() {
  document.getElementById("stat-total-sessions").textContent = sessions.length;

  let facts = [];
  try {
    facts = await api(`/users/${userId}/facts`);
  } catch (e) {
    console.error("Failed to load facts", e);
  }
  document.getElementById("stat-active-facts").textContent = facts.length;

  document.getElementById("dashboard-memory").innerHTML = facts.length
    ? `<div class="ms-block">
        <div class="ms-title">Top Facts</div>
        <div class="fact-pills">
          ${facts.slice(0, 3).map(f => `<div class="fact-pill"><span class="cat">${escapeHtml(f.category || "general")}</span>${escapeHtml(f.content)}</div>`).join("")}
        </div>
      </div>`
    : `<div class="ms-block"><div class="ms-title">Top Facts</div><p class="session-empty">No facts learned yet.</p></div>`;

  await populateMemorySessionSelect();
}

async function populateMemorySessionSelect() {
  const select = document.getElementById("memory-session-select");
  const panel = document.getElementById("session-memory-panel");
  const recent = sessions.slice(0, 10);

  if (!recent.length) {
    panel.hidden = true;
    select.innerHTML = "";
    memorySessionId = null;
    memoryData = null;
    return;
  }

  select.innerHTML = recent.map(s => `
    <option value="${s.id}">${escapeHtml(s.title || "Untitled")}</option>
  `).join("");

  const stillPresent = recent.some(s => s.id === memorySessionId);
  if (!stillPresent) memorySessionId = recent[0].id;
  select.value = memorySessionId;

  await loadMemoryForSession(memorySessionId);
}

async function loadMemoryForSession(id) {
  try {
    memoryData = await api(`/sessions/${id}/memory`);
  } catch (e) {
    console.error("Failed to load memory", e);
    memoryData = null;
  }
  renderMemoryColumn();
}

function renderSessionList() {
  const list = document.getElementById("session-list");
  if (sessionsLoadError) {
    list.innerHTML = `<div class="session-empty">Can't reach the backend at ${API_BASE}</div>`;
    return;
  }
  const query = searchQuery.trim().toLowerCase();
  const filtered = query
    ? sessions.filter(s => (s.title || "").toLowerCase().includes(query))
    : sessions;

  if (!filtered.length) {
    list.innerHTML = query
      ? `<div class="session-empty">No chats match "${escapeHtml(searchQuery)}"</div>`
      : `<div class="session-empty">No chats yet</div>`;
    return;
  }

  list.innerHTML = filtered.map(s => `
    <div class="session-item ${s.id === activeSessionId ? "active" : ""} ${s.id === openSessionMenuId ? "menu-open" : ""}" data-select-session="${s.id}">
      <div class="session-meta">
        <div class="session-title-row">
          <span class="session-title">${escapeHtml(s.title || "Untitled")}</span>
          <div class="session-actions">
            <button class="session-action-btn" data-menu-session="${s.id}" title="More options">${DOTS_ICON_SVG}</button>
            <div class="session-menu" ${s.id === openSessionMenuId ? "" : "hidden"}>
              <button class="session-menu-item" data-rename-session="${s.id}">${RENAME_ICON_SVG}Rename</button>
              <button class="session-menu-item session-menu-item-danger" data-delete-session="${s.id}">${DELETE_ICON_SVG}Delete</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  `).join("");
}

function closeSessionMenu() {
  if (openSessionMenuId !== null) {
    openSessionMenuId = null;
    renderSessionList();
  }
}

// Edits the title in place via direct DOM manipulation rather than a full re-render, so the
// input keeps focus/cursor position while typing. Enter or blur saves, Escape reverts.
function startRenameSession(id) {
  const item = document.querySelector(`.session-item[data-select-session="${id}"]`);
  const session = sessions.find(s => s.id === id);
  if (!item || !session) return;
  const titleEl = item.querySelector(".session-title");
  if (!titleEl) return;

  const input = document.createElement("input");
  input.className = "session-title-input";
  input.value = session.title || "";
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  const finish = (save) => {
    input.removeEventListener("keydown", onKeydown);
    input.removeEventListener("blur", onBlur);
    if (save) saveSessionRename(id, input.value);
    else renderSessionList();
  };
  const onKeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  const onBlur = () => finish(true);
  input.addEventListener("keydown", onKeydown);
  input.addEventListener("blur", onBlur);
}

async function saveSessionRename(id, rawTitle) {
  const session = sessions.find(s => s.id === id);
  if (!session) return;
  const title = rawTitle.trim();
  if (!title || title === session.title) { renderSessionList(); return; }

  const previous = session.title;
  session.title = title;
  renderSessionList();

  try {
    await api(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
  } catch (e) {
    console.error("Failed to rename session", e);
    session.title = previous;
    renderSessionList();
  }
}

async function deleteSessionPrompt(id) {
  const session = sessions.find(s => s.id === id);
  if (!confirm(`Delete "${session?.title || "this chat"}"? This can't be undone.`)) return;

  try {
    await api(`/sessions/${id}`, { method: "DELETE" });
  } catch (e) {
    console.error("Failed to delete session", e);
    return;
  }

  sessions = sessions.filter(s => s.id !== id);
  renderSessionList();
  if (activeSessionId === id) startNewChat();
}

function updateMainHeader() {
  const header = document.getElementById("main-header");
  if (currentView === "chat") {
    header.hidden = true;
  } else {
    header.hidden = false;
    document.getElementById("main-title").innerHTML = `Memory <span class="crumb-sep">/</span> <span class="crumb-sub">Sessions, memory, and recent activity</span>`;
  }
}

function updateNavActive() {
  document.querySelectorAll(".nav-item").forEach(el => {
    el.classList.toggle("active", el.dataset.view === currentView);
  });
}

function applyTheme(theme) {
  currentTheme = theme;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  document.getElementById("theme-toggle-icon").innerHTML = theme === "dark" ? SUN_ICON_PATH : MOON_ICON_PATH;
}

function updateChatSubview() {
  const showHero = activeSessionId === null;
  document.getElementById("chat-hero").hidden = !showHero;
  document.getElementById("chat-thread").hidden = showHero;
}

const REGEN_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`;
const THUMBS_UP_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M7 22V11M2 13v7a2 2 0 0 0 2 2h12.5a2 2 0 0 0 2-1.7l1.4-9A2 2 0 0 0 18 9H14V4a2 2 0 0 0-2-2l-5 7"/></svg>`;
const THUMBS_DOWN_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M17 2v11M22 11V4a2 2 0 0 0-2-2H7.5a2 2 0 0 0-2 1.7l-1.4 9A2 2 0 0 0 6 15h4v5a2 2 0 0 0 2 2l5-7"/></svg>`;
const COPY_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const CHECK_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>`;
const RENAME_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`;
const DELETE_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0-1 14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2L4 6h16Z"/></svg>`;
const DOTS_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>`;

// Only server-persisted messages (m.id set) can carry feedback — a reply that's still
// streaming has no id yet (see setMessageFeedback's comment) so the buttons stay hidden
// until the message_id SSE event lands and triggers a re-render.
function renderThread({ keepScroll } = {}) {
  const messages = document.getElementById("messages");
  const lastIndex = activeMessages.length - 1;
  const loadEarlierHtml = hasMoreMessages
    ? `<div class="load-earlier-row"><button class="load-earlier-btn" data-load-earlier>Load earlier messages</button></div>`
    : "";
  messages.innerHTML = loadEarlierHtml + activeMessages.map((m, i) => {
    const showRegen = m.role === "assistant" && i === lastIndex && !isStreaming;
    const showFeedback = m.role === "assistant" && !!m.id;
    const showCopy = m.role === "assistant" && !!m.content;
    return `
    <div class="msg-row ${m.role}">
      ${m.role === "assistant" ? `<div class="msg-col">
        <div class="bubble markdown-body">${renderMarkdown(m.content)}</div>
        ${showRegen || showFeedback || showCopy ? `<div class="msg-actions">
          ${showCopy ? `<button class="feedback-btn copy-btn" data-copy-index="${i}" title="Copy">${COPY_ICON_SVG}</button>` : ""}
          ${showFeedback ? `
            <button class="feedback-btn ${m.feedback === "like" ? "is-active" : ""}" data-feedback="like" data-message-id="${m.id}" title="Good response">${THUMBS_UP_ICON_SVG}</button>
            <button class="feedback-btn ${m.feedback === "dislike" ? "is-active" : ""}" data-feedback="dislike" data-message-id="${m.id}" title="Bad response">${THUMBS_DOWN_ICON_SVG}</button>
          ` : ""}
          ${showRegen ? `<button class="regen-btn" data-regenerate title="Regenerate response">${REGEN_ICON_SVG}</button>` : ""}
        </div>` : ""}
      </div>` : `<div class="bubble">${escapeHtml(m.content).replace(/\n/g, "<br>")}</div>`}
    </div>
  `;
  }).join("");
  // Callers doing a scroll-preserving prepend (loadEarlierMessages) set scrollTop themselves
  // right after this call — jumping to the bottom here would just be immediately overwritten,
  // but skipping it unconditionally would break the normal case (new/streamed messages).
  if (!keepScroll) messages.scrollTop = messages.scrollHeight;
}

function renderMemoryColumn() {
  const panel = document.getElementById("session-memory-panel");
  if (!memorySessionId || !memoryData) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  document.getElementById("memory-summary").textContent =
    memoryData.summary || "No summary yet — this session just started.";

  const b = memoryData.budget;
  const used = {
    recent: b.recent.used_tokens,
    summary: b.summary.used_tokens,
    facts: b.facts.used_tokens,
    system: b.system.used_tokens,
  };
  const total = used.recent + used.summary + used.facts + used.system || 1;
  document.getElementById("budget-bar").innerHTML = `
    <div class="budget-seg recent" style="width:${(used.recent / total) * 100}%"></div>
    <div class="budget-seg summary" style="width:${(used.summary / total) * 100}%"></div>
    <div class="budget-seg facts" style="width:${(used.facts / total) * 100}%"></div>
    <div class="budget-seg system" style="width:${(used.system / total) * 100}%"></div>
  `;
  document.getElementById("budget-legend").innerHTML = `
    <div class="budget-legend-item"><span class="budget-legend-swatch" style="background:var(--accent-dark)"></span>Recent turns<span class="amt">${used.recent.toLocaleString()}</span></div>
    <div class="budget-legend-item"><span class="budget-legend-swatch" style="background:var(--accent)"></span>Rolling summary<span class="amt">${used.summary.toLocaleString()}</span></div>
    <div class="budget-legend-item"><span class="budget-legend-swatch" style="background:var(--accent-light)"></span>Injected facts<span class="amt">${used.facts.toLocaleString()}</span></div>
    <div class="budget-legend-item"><span class="budget-legend-swatch" style="background:var(--gray-border)"></span>System prompt<span class="amt">${used.system.toLocaleString()}</span></div>
  `;

  document.getElementById("memory-facts").innerHTML = memoryData.injected_facts.length
    ? memoryData.injected_facts.map(f => `
        <div class="fact-pill"><span class="cat">${escapeHtml(f.category || "general")}</span>${escapeHtml(f.content)}</div>
      `).join("")
    : `<span class="session-empty">No facts injected into this turn.</span>`;
}

function switchView(view) {
  currentView = view;
  document.querySelectorAll(".view").forEach(v => v.hidden = true);
  document.getElementById(`view-${view}`).hidden = false;
  if (view === "chat") updateChatSubview();
  if (view === "memory") renderDashboard();
  updateNavActive();
  updateMainHeader();
}

async function selectSession(id, { pushUrl = true } = {}) {
  activeSessionId = id;
  activeMessages = [];
  hasMoreMessages = false;
  renderSessionList();
  renderThread();
  updateChatSubview();
  updateMainHeader();
  if (pushUrl) updateUrlForRoute("chat", id);

  try {
    const { messages, hasMore } = await fetchMessages(id);
    activeMessages = messages.map(m => ({ role: m.role, content: m.content, id: m.id, feedback: m.feedback }));
    hasMoreMessages = hasMore;
  } catch (e) {
    console.error("Failed to load messages", e);
  }
  renderThread();
}

async function loadEarlierMessages() {
  if (!hasMoreMessages || !activeSessionId || !activeMessages.length) return;
  const sessionId = activeSessionId;
  const oldestId = activeMessages[0].id;
  if (!oldestId) return; // nothing loaded from the server yet to anchor a cursor on

  try {
    const { messages, hasMore } = await fetchMessages(sessionId, { before: oldestId });
    if (activeSessionId !== sessionId) return; // user navigated away while this was in flight
    activeMessages = [...messages.map(m => ({ role: m.role, content: m.content, id: m.id, feedback: m.feedback })), ...activeMessages];
    hasMoreMessages = hasMore;

    const messagesEl = document.getElementById("messages");
    const prevScrollTop = messagesEl.scrollTop;
    const prevScrollHeight = messagesEl.scrollHeight;
    renderThread({ keepScroll: true });
    messagesEl.scrollTop = prevScrollTop + (messagesEl.scrollHeight - prevScrollHeight);
  } catch (e) {
    console.error("Failed to load earlier messages", e);
  }
}

function startNewChat({ pushUrl = true } = {}) {
  activeSessionId = null;
  activeMessages = [];
  hasMoreMessages = false;
  renderSessionList();
  renderThread();
  switchView("chat");
  if (pushUrl) updateUrlForRoute("chat", null);
}

let isStreaming = false;
let streamingSessionId = null;

// Shared by sendChatMessage() and regenerateLastResponse() — both consume an SSE stream of
// `data:` deltas into `assistantMsg`, and both need to react the same way to `error`,
// `done`, and `cancelled` events.
async function streamAssistantReply(url, body, assistantMsg, sessionId) {
  let wasCancelled = false;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-User-Id": userId },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok || !res.body) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch {}
      throw new Error(detail || `request failed: ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        let event = "message";
        let data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
          else if (line) data += line;
        }
        if (event === "error") throw new Error(data || "stream error");
        if (event === "done") continue;
        if (event === "cancelled") { wasCancelled = true; continue; }
        if (event === "message_id") {
          assistantMsg.id = data;
          if (activeSessionId === sessionId) renderThread(); // reveal feedback buttons now that there's an id to attach them to
          continue;
        }
        if (activeSessionId === sessionId) {
          assistantMsg.content += data;
          renderThread();
        }
      }
    }
    if (wasCancelled) {
      assistantMsg.content += assistantMsg.content ? "\n\n[stopped]" : "[stopped]";
      if (activeSessionId === sessionId) renderThread();
    }
  } catch (err) {
    console.error("Chat stream failed", err);
    assistantMsg.content += `\n\n[error: ${err.message}]`;
    if (activeSessionId === sessionId) renderThread();
  }
}

async function sendChatMessage(text) {
  const sessionId = activeSessionId;
  activeMessages.push({ role: "user", content: text });
  const assistantMsg = { role: "assistant", content: "" };
  activeMessages.push(assistantMsg);
  renderThread();

  isStreaming = true;
  streamingSessionId = sessionId;
  setComposerStreaming(true);

  await streamAssistantReply(`${API_BASE}/sessions/${sessionId}/chat`, { message: text }, assistantMsg, sessionId);

  isStreaming = false;
  streamingSessionId = null;
  setComposerStreaming(false);
  if (activeSessionId === sessionId) renderThread(); // last render during the loop still had isStreaming true, so the regen button wasn't drawn yet

  await loadSessions();

  // First turn: the backend generates a real title in a background task after this
  // response, so the placeholder we just loaded is likely stale. Re-check a few times
  // with backoff and pick up the real title once it lands.
  if (activeMessages.length === 2) {
    const placeholderTitle = sessions.find(s => s.id === sessionId)?.title;
    pollForTitleUpdate(sessionId, placeholderTitle);
  }
}

async function pollForTitleUpdate(sessionId, placeholderTitle, attempt = 0) {
  const delays = [1500, 2500, 4000];
  if (attempt >= delays.length) return;
  await new Promise(resolve => setTimeout(resolve, delays[attempt]));
  await loadSessions();
  const currentTitle = sessions.find(s => s.id === sessionId)?.title;
  if (currentTitle !== placeholderTitle) return;
  await pollForTitleUpdate(sessionId, placeholderTitle, attempt + 1);
}

async function regenerateLastResponse() {
  if (isStreaming || !activeSessionId) return;
  const last = activeMessages[activeMessages.length - 1];
  if (!last || last.role !== "assistant") return;
  const sessionId = activeSessionId;

  activeMessages.pop();
  const assistantMsg = { role: "assistant", content: "" };
  activeMessages.push(assistantMsg);
  renderThread();

  isStreaming = true;
  streamingSessionId = sessionId;
  setComposerStreaming(true);

  await streamAssistantReply(`${API_BASE}/sessions/${sessionId}/regenerate`, undefined, assistantMsg, sessionId);

  isStreaming = false;
  streamingSessionId = null;
  setComposerStreaming(false);
  if (activeSessionId === sessionId) renderThread();
}

// Clicking an already-active thumb clears feedback (toggle); clicking the other one
// switches it — the backend just overwrites with whatever value is sent, no toggle logic
// there. Optimistic update + re-render first so the click feels instant; reverted on failure.
async function setMessageFeedback(messageId, value) {
  const sessionId = activeSessionId;
  const message = activeMessages.find(m => m.id === messageId);
  if (!message) return;

  const previous = message.feedback ?? null;
  const next = previous === value ? null : value;
  message.feedback = next;
  renderThread();

  try {
    await api(`/sessions/${sessionId}/messages/${messageId}/feedback`, {
      method: "PATCH",
      body: JSON.stringify({ feedback: next }),
    });
  } catch (e) {
    console.error("Failed to save feedback", e);
    message.feedback = previous;
    if (activeSessionId === sessionId) renderThread();
  }
}

// Copies the raw markdown source (not the rendered HTML) — pasting into another chat, an
// editor, or an issue should reproduce what the model actually said, not lose formatting to
// stray tags. Swaps the icon in place for a moment rather than going through renderThread(),
// since this is a transient UI confirmation, not state worth re-rendering the thread for.
function copyMessageContent(button) {
  const message = activeMessages[Number(button.dataset.copyIndex)];
  if (!message) return;
  navigator.clipboard.writeText(message.content).then(() => {
    const original = button.innerHTML;
    button.innerHTML = CHECK_ICON_SVG;
    button.classList.add("is-copied");
    setTimeout(() => {
      button.innerHTML = original;
      button.classList.remove("is-copied");
    }, 1200);
  }).catch(e => console.error("Failed to copy message", e));
}

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

const SEND_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7Z"/></svg>Send`;
const STOP_ICON_SVG = `<svg class="icon" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>Stop`;

function setComposerStreaming(streaming) {
  const btn = document.getElementById("send-btn");
  btn.classList.toggle("is-stop", streaming);
  btn.innerHTML = streaming ? STOP_ICON_SVG : SEND_ICON_SVG;
}

function stopGenerating() {
  if (!streamingSessionId) return;
  api(`/sessions/${streamingSessionId}/chat/cancel`, { method: "POST" }).catch(e =>
    console.error("Failed to cancel generation", e)
  );
}

async function sendMessage() {
  if (isStreaming) { stopGenerating(); return; }
  const input = document.getElementById("composer-input");
  const text = input.value.trim();
  if (!text || !activeSessionId) return;
  input.value = "";
  autoResize(input);
  await sendChatMessage(text);
}

async function sendHeroMessage() {
  const input = document.getElementById("hero-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autoResize(input);

  let session;
  try {
    session = await api("/sessions", {
      method: "POST",
      body: JSON.stringify({ title: text.length > 40 ? text.slice(0, 40) + "…" : text }),
    });
  } catch (e) {
    console.error("Failed to create session", e);
    return;
  }

  sessions.unshift(session);
  activeSessionId = session.id;
  activeMessages = [];
  hasMoreMessages = false;
  renderSessionList();
  updateChatSubview();
  updateUrlForRoute("chat", session.id);
  await sendChatMessage(text);
}

document.addEventListener("click", (e) => {
  // Closes any open session "..." menu on a click anywhere outside it, before any other
  // routing below runs — except on the trigger/menu itself, which handle their own state.
  if (openSessionMenuId !== null && !e.target.closest(".session-menu") && !e.target.closest("[data-menu-session]")) {
    closeSessionMenu();
  }

  const navItem = e.target.closest(".nav-item, .brand");
  if (navItem) {
    switchView(navItem.dataset.view);
    updateUrlForRoute(navItem.dataset.view, activeSessionId);
    return;
  }

  // Checked before [data-select-session] below since everything here lives inside a
  // session-item that also carries that attribute — closest() would otherwise match the
  // outer item first.
  const menuTrigger = e.target.closest("[data-menu-session]");
  if (menuTrigger) {
    const id = menuTrigger.dataset.menuSession;
    openSessionMenuId = openSessionMenuId === id ? null : id;
    renderSessionList();
    return;
  }

  const renameBtn = e.target.closest("[data-rename-session]");
  if (renameBtn) {
    const id = renameBtn.dataset.renameSession;
    openSessionMenuId = null;
    renderSessionList(); // closes the menu before swapping the title for an input
    startRenameSession(id);
    return;
  }

  const deleteBtn = e.target.closest("[data-delete-session]");
  if (deleteBtn) {
    const id = deleteBtn.dataset.deleteSession;
    openSessionMenuId = null;
    renderSessionList(); // closes the menu regardless of what the confirm() dialog decides
    deleteSessionPrompt(id);
    return;
  }

  const selectItem = e.target.closest("[data-select-session]");
  if (selectItem) {
    switchView("chat");
    selectSession(selectItem.dataset.selectSession);
    return;
  }

  if (e.target.closest("#send-btn")) { sendMessage(); return; }
  if (e.target.closest("#hero-send-btn")) { sendHeroMessage(); return; }
  if (e.target.closest("[data-regenerate]")) { regenerateLastResponse(); return; }
  if (e.target.closest("[data-load-earlier]")) { loadEarlierMessages(); return; }

  const feedbackBtn = e.target.closest("[data-feedback]");
  if (feedbackBtn) {
    setMessageFeedback(feedbackBtn.dataset.messageId, feedbackBtn.dataset.feedback);
    return;
  }

  const copyBtn = e.target.closest("[data-copy-index]");
  if (copyBtn) {
    copyMessageContent(copyBtn);
    return;
  }

  const heroPill = e.target.closest(".hero-pill");
  if (heroPill) {
    const input = document.getElementById("hero-input");
    input.value = heroPill.dataset.prompt;
    autoResize(input);
    input.focus();
    return;
  }

  if (e.target.closest("#new-chat-btn")) { startNewChat(); return; }

  if (e.target.closest("#theme-toggle")) {
    applyTheme(currentTheme === "dark" ? "light" : "dark");
    return;
  }
});

document.getElementById("composer-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
document.getElementById("composer-input").addEventListener("input", (e) => autoResize(e.target));

document.getElementById("hero-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendHeroMessage(); }
});
document.getElementById("hero-input").addEventListener("input", (e) => autoResize(e.target));

document.getElementById("chat-search").addEventListener("input", (e) => {
  searchQuery = e.target.value;
  renderSessionList();
});

document.getElementById("memory-session-select").addEventListener("change", (e) => {
  memorySessionId = e.target.value;
  loadMemoryForSession(memorySessionId);
});

window.addEventListener("popstate", () => {
  const { view, sessionId } = getRouteFromPath();
  switchView(view);
  if (view === "chat") {
    if (sessionId) selectSession(sessionId, { pushUrl: false });
    else startNewChat({ pushUrl: false });
  }
});

async function init() {
  updateNavActive();
  updateMainHeader();
  updateChatSubview();
  renderThread();
  renderMemoryColumn();
  applyTheme(currentTheme);
  await loadSessions();
  await renderDashboard();

  const { view, sessionId, matched } = getRouteFromPath();
  if (view === "memory") {
    switchView("memory");
    updateUrlForRoute("memory", null, { push: false });
  } else if (sessionId && sessions.some(s => s.id === sessionId)) {
    await selectSession(sessionId, { pushUrl: false });
  } else if (matched) {
    // Either "/chat/" (no id) or a stale/unknown id — settle on the canonical new-chat path.
    updateUrlForRoute("chat", null, { push: false });
  }
}
init();
