"use strict";

/*
 * Takkub Remote — vanilla JS SPA, no build step, no external requests.
 * Data-minimization contract (design doc §7.3): only ever render (1) the
 * Lead's own conversation/report text and (2) a working/total count. Any
 * other field the API might accidentally include (role, task, pane id,
 * transcript, state) is never read into the DOM, even defensively.
 */
(function () {
  var LS_TOKEN = "takkub_remote_token";
  var LS_BASE = "takkub_remote_base";

  var state = {
    token: localStorage.getItem(LS_TOKEN) || "",
    base: localStorage.getItem(LS_BASE) || "",
    mode: "view",
    view: "lead",
    es: null,
    esRetries: 0,
    esTimer: null,
    pulseTimer: null,
    lastToast: 0,
  };

  var $ = function (id) { return document.getElementById(id); };
  var appEl = $("app");

  // ---------------------------------------------------------------
  // Base URL + token bootstrap
  // ---------------------------------------------------------------

  function dirOf(pathname) {
    var i = pathname.lastIndexOf("/");
    return i >= 0 ? pathname.slice(0, i + 1) : "/";
  }

  function parsePairingUrl(raw) {
    try {
      var u = new URL(raw);
      var hash = u.hash || "";
      var m = /(?:^|[#&])token=([^&]+)/.exec(hash);
      if (!m) return null;
      var token = decodeURIComponent(m[1]);
      var base = u.origin + dirOf(u.pathname);
      return { token: token, base: base };
    } catch (e) {
      return null;
    }
  }

  function bootstrapFromLocation() {
    // Loaded via a fresh pairing link: https://host/<secret>/#token=...
    if (location.hash && /token=/.test(location.hash)) {
      var parsed = parsePairingUrl(location.href);
      if (parsed) {
        state.token = parsed.token;
        state.base = parsed.base;
        localStorage.setItem(LS_TOKEN, state.token);
        localStorage.setItem(LS_BASE, state.base);
        // strip the fragment so the token never lingers in browser history
        history.replaceState(null, "", location.pathname + location.search);
        return;
      }
    }
    if (!state.base) {
      // installed PWA / plain reload under the secret path, no fragment.
      state.base = location.origin + dirOf(location.pathname);
    }
  }

  bootstrapFromLocation();

  // ---------------------------------------------------------------
  // API helper
  // ---------------------------------------------------------------

  function apiUrl(path) {
    var base = state.base || "./";
    if (base.charAt(base.length - 1) !== "/") base += "/";
    return base + path.replace(/^\/+/, "");
  }

  function apiFetch(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    var hadToken = !!state.token;
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    return fetch(apiUrl(path), Object.assign({}, opts, { headers: headers }))
      .then(function (res) {
        setOffline(false);
        // Server is a zero-surface design: every auth failure (bad secret
        // path, bad/expired token) answers with a bare 404 — never a 401.
        // A 404 on an /api/ call while we believe we hold a token means the
        // token is no longer valid (server restarted, rotated, revoked).
        if (res.status === 404 && hadToken && /^api\//.test(path.replace(/^\/+/, ""))) {
          forgetToken();
          throw new Error("unauthorized");
        }
        // Third auth factor (addendum): a cockpit-set password gates every
        // authenticated route until verified — never encoded in the pairing
        // URL/QR, so a leaked link alone can't get in. `res.clone()` so the
        // caller can still read the original body when this isn't the gate.
        if (res.status === 403) {
          return res
            .clone()
            .json()
            .then(function (data) {
              if (data && data.msg === "password_required") {
                showPasswordPrompt();
                throw new Error("password_required");
              }
              return res;
            })
            .catch(function (err) {
              if (err instanceof Error && err.message === "password_required") throw err;
              return res;
            });
        }
        return res;
      })
      .catch(function (err) {
        if (err instanceof TypeError) setOffline(true);
        throw err;
      });
  }

  function forgetToken() {
    state.token = "";
    localStorage.removeItem(LS_TOKEN);
    showPairing("session หมดอายุ หรือ token ไม่ถูกต้อง — สแกน QR ใหม่");
  }

  // ---------------------------------------------------------------
  // Offline banner
  // ---------------------------------------------------------------

  var offlineBanner = $("offline-banner");
  var statusConn = $("status-conn");
  var statusConnText = $("status-conn-text");
  var isOffline = false;
  function setOffline(v) {
    if (v === isOffline) return;
    isOffline = v;
    offlineBanner.classList.toggle("show", v);
    statusConn.classList.toggle("offline", v);
    statusConnText.textContent = v ? "Offline" : "Online";
  }

  // ---------------------------------------------------------------
  // Toast
  // ---------------------------------------------------------------

  function toast(msg) {
    var el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    var mine = ++state.lastToast;
    setTimeout(function () {
      if (mine === state.lastToast) el.classList.remove("show");
    }, 2600);
  }

  // ---------------------------------------------------------------
  // View switching
  // ---------------------------------------------------------------

  function showPairing(errorMsg) {
    appEl.classList.remove("gate-mode");
    appEl.classList.add("pairing-mode");
    document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
    $("view-pairing").classList.add("active");
    $("pairing-error").textContent = errorMsg || "";
    stopLeadStream();
    stopPulsePolling();
  }

  // Third auth factor (addendum): shown whenever the server answers an
  // authenticated call with 403 password_required. Never reachable via the
  // pairing URL/QR alone — the password is asked for here, not embedded
  // anywhere in the link.
  function showPasswordPrompt(errorMsg) {
    appEl.classList.remove("pairing-mode");
    appEl.classList.add("gate-mode");
    document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
    $("view-password").classList.add("active");
    $("password-error").textContent = errorMsg || "";
    stopLeadStream();
    stopPulsePolling();
  }

  function showApp() {
    appEl.classList.remove("pairing-mode", "gate-mode");
    switchView(state.view);
  }

  function switchView(name) {
    state.view = name;
    document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
    var el = $("view-" + name);
    if (el) el.classList.add("active");
    document.querySelectorAll("#bottom-nav button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.view === name);
    });
    if (name === "projects") loadProjects();
    if (name === "lead") startLeadStream();
    if (name === "pulse") startPulsePolling();
    else stopPulsePolling();
    if (name !== "lead") stopLeadStream();
  }

  document.querySelectorAll("#bottom-nav button").forEach(function (btn) {
    btn.addEventListener("click", function () { switchView(btn.dataset.view); });
  });

  // ---------------------------------------------------------------
  // Pairing screen
  // ---------------------------------------------------------------

  $("pairing-connect").addEventListener("click", function () {
    var raw = $("pairing-url").value.trim();
    if (!raw) {
      $("pairing-error").textContent = "วางลิงก์จับคู่ก่อน";
      return;
    }
    var parsed = parsePairingUrl(raw);
    if (!parsed) {
      $("pairing-error").textContent = "ลิงก์ไม่ถูกต้อง ต้องมี #token=...";
      return;
    }
    state.token = parsed.token;
    state.base = parsed.base;
    localStorage.setItem(LS_TOKEN, state.token);
    localStorage.setItem(LS_BASE, state.base);
    $("pairing-error").textContent = "";
    init();
  });

  // ---------------------------------------------------------------
  // Password gate (third auth factor — never sent via URL/QR)
  // ---------------------------------------------------------------

  $("password-form").addEventListener("submit", function (evt) {
    evt.preventDefault();
    var input = $("password-input");
    var password = input.value;
    if (!password) return;
    apiFetch("api/verify-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password }),
    })
      .then(function (r) {
        input.value = "";
        if (r.ok) {
          showApp();
          fetchProjectsAndMode().catch(function () { /* stay in view mode assumption */ });
        } else {
          $("password-error").textContent = "รหัสผ่านไม่ถูกต้อง";
        }
      })
      .catch(function () {
        $("password-error").textContent = "เชื่อมต่อไม่ได้ ลองใหม่อีกครั้ง";
      });
  });

  // ---------------------------------------------------------------
  // Projects
  // ---------------------------------------------------------------

  // Fetches /api/projects once: feeds both the projects list and the
  // view/control mode (P1 has no dedicated mode endpoint — the same
  // response carries `mode`, so this is the single source of truth).
  function fetchProjectsAndMode() {
    return apiFetch("api/projects")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && (data.mode === "view" || data.mode === "control")) {
          state.mode = data.mode;
          updateControlNote();
        }
        return Array.isArray(data && data.projects) ? data.projects : [];
      });
  }

  function loadProjects() {
    var list = $("projects-list");
    list.innerHTML = '<div class="empty-state">กำลังโหลด…</div>';
    fetchProjectsAndMode()
      .then(renderProjects)
      .catch(function () {
        list.innerHTML = '<div class="empty-state">โหลด projects ไม่สำเร็จ<br>ลองใหม่อีกครั้ง</div>';
      });
  }

  // P1 is read-only: no project/open route exists on the server, so this
  // only ever renders the list + an "active" indicator — no click-to-switch.
  function renderProjects(items) {
    var list = $("projects-list");
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">ยังไม่มี project ที่ import ไว้</div>';
      return;
    }
    items.forEach(function (item) {
      // item may be a plain string or {name, active}
      var name = typeof item === "string" ? item : (item && item.name) || "";
      if (!name) return;
      var active = typeof item === "object" && item.active;
      var row = document.createElement("div");
      row.className = "project-row" + (active ? " active" : "");
      var iconBox = document.createElement("span");
      iconBox.className = "icon-box";
      iconBox.textContent = active ? "📂" : "📁";
      row.appendChild(iconBox);
      var nameCol = document.createElement("div");
      nameCol.className = "name-col";
      var label = document.createElement("span");
      label.className = "name";
      label.textContent = name;
      nameCol.appendChild(label);
      var hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = "อ่านอย่างเดียว — สลับ project ที่ cockpit";
      nameCol.appendChild(hint);
      row.appendChild(nameCol);
      var tag = document.createElement("span");
      tag.className = "tag" + (active ? "" : " idle");
      tag.textContent = active ? "active" : "idle";
      row.appendChild(tag);
      list.appendChild(row);
    });
  }

  // ---------------------------------------------------------------
  // Lead console (SSE)
  // ---------------------------------------------------------------

  var MAX_ES_RETRIES = 5;

  function appendMsg(kind, text) {
    if (typeof text !== "string" || !text) return;
    var log = $("lead-log");
    var emptyEl = $("lead-empty");
    if (emptyEl) emptyEl.remove();
    var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    var div = document.createElement("div");
    div.className = "msg " + kind;
    div.textContent = text;
    log.appendChild(div);
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function startLeadStream() {
    updateControlNote();
    if (state.es || state.esTimer) return;
    requestTicketAndConnect();
  }

  function requestTicketAndConnect() {
    apiFetch("api/sse-ticket", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var ticket = data && data.ticket;
        if (!ticket) throw new Error("no ticket");
        connectSse(ticket);
      })
      .catch(function () {
        scheduleEsRetry();
      });
  }

  // data-min: only ever surface the Lead's own text field, whether the
  // server sends a bare string or a JSON-wrapped {text|message} payload.
  function parseSseData(raw) {
    try {
      var payload = JSON.parse(raw);
      if (typeof payload === "string") return payload;
      if (payload && typeof payload === "object") {
        var text = payload.text || payload.message;
        return typeof text === "string" ? text : null;
      }
      return null;
    } catch (e) {
      return typeof raw === "string" ? raw : null;
    }
  }

  function connectSse(ticket) {
    var url = apiUrl("api/lead?ticket=" + encodeURIComponent(ticket));
    var es = new EventSource(url);
    state.es = es;
    es.onopen = function () {
      state.esRetries = 0;
    };
    // Server emits named SSE events (`event: lead` / `event: done`), never
    // the default unnamed "message" type — must use addEventListener.
    es.addEventListener("lead", function (evt) {
      appendMsg("lead", parseSseData(evt.data));
    });
    es.addEventListener("done", function (evt) {
      appendMsg("sys", parseSseData(evt.data));
    });
    es.onerror = function () {
      es.close();
      state.es = null;
      scheduleEsRetry();
    };
  }

  function scheduleEsRetry() {
    if (state.view !== "lead") return;
    state.esRetries += 1;
    if (state.esRetries > MAX_ES_RETRIES) {
      appendMsg("sys", "เชื่อมต่อ Lead ไม่ได้ — tunnel URL อาจเปลี่ยน สแกน QR ใหม่");
      return;
    }
    var delay = Math.min(1000 * Math.pow(2, state.esRetries), 15000);
    state.esTimer = setTimeout(function () {
      state.esTimer = null;
      requestTicketAndConnect();
    }, delay);
  }

  function stopLeadStream() {
    if (state.es) { state.es.close(); state.es = null; }
    if (state.esTimer) { clearTimeout(state.esTimer); state.esTimer = null; }
    state.esRetries = 0;
  }

  function updateControlNote() {
    var isControl = state.mode === "control";
    $("view-control-note").textContent = isControl
      ? ""
      : "โหมด view — ส่งข้อความไม่ได้ · เปิด control ได้จาก cockpit บนเดสก์ท็อป";
    $("lead-send").disabled = !isControl;
    var input = $("lead-input");
    input.disabled = !isControl;
    input.placeholder = isControl ? "พิมพ์ถึง Lead…" : "อ่านอย่างเดียว (view mode)";
    var modePill = $("status-mode");
    modePill.textContent = isControl ? "CONTROL" : "VIEW";
    modePill.classList.toggle("control", isControl);
  }

  $("lead-composer").addEventListener("submit", function (evt) {
    evt.preventDefault();
    var input = $("lead-input");
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    autosizeInput();
    appendMsg("me", text);
    apiFetch("api/lead/say", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    }).catch(function () {
      toast("ส่งข้อความไม่สำเร็จ");
    });
  });

  var leadInput = $("lead-input");
  function autosizeInput() {
    leadInput.style.height = "auto";
    leadInput.style.height = Math.min(leadInput.scrollHeight, 100) + "px";
  }
  leadInput.addEventListener("input", autosizeInput);
  leadInput.addEventListener("keydown", function (evt) {
    if (evt.key === "Enter" && !evt.shiftKey) {
      evt.preventDefault();
      $("lead-composer").dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  // ---------------------------------------------------------------
  // Pulse
  // ---------------------------------------------------------------

  function startPulsePolling() {
    fetchPulse();
    stopPulsePolling();
    state.pulseTimer = setInterval(fetchPulse, 5000);
  }

  function stopPulsePolling() {
    if (state.pulseTimer) { clearInterval(state.pulseTimer); state.pulseTimer = null; }
  }

  function fetchPulse() {
    apiFetch("api/pulse")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // data-min: pull only numeric working/total, ignore anything else.
        var working = Number(data && data.working);
        var total = Number(data && data.total);
        renderPulse(Number.isFinite(working) ? working : 0, Number.isFinite(total) ? total : 0);
      })
      .catch(function () { /* keep last known value on transient failure */ });
  }

  function renderPulse(working, total) {
    $("pulse-working").textContent = String(working);
    $("pulse-total").textContent = "จาก " + total + " ตำแหน่งที่เปิด";
    var pct = total > 0 ? Math.min(100, Math.round((working / total) * 100)) : 0;
    $("pulse-ring").style.background =
      "conic-gradient(var(--work) " + (pct * 3.6) + "deg, var(--line) 0deg)";
  }

  // ---------------------------------------------------------------
  // Service worker
  // ---------------------------------------------------------------

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("sw.js").catch(function () {});
    });
  }

  // ---------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------

  function init() {
    if (!state.token) {
      showPairing();
      return;
    }
    showApp();
    fetchProjectsAndMode().catch(function () { /* stay in view mode assumption */ });
  }

  init();
})();
