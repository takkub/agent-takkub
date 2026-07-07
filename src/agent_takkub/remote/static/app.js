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
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    return fetch(apiUrl(path), Object.assign({}, opts, { headers: headers }))
      .then(function (res) {
        setOffline(false);
        if (res.status === 401) {
          forgetToken();
          throw new Error("unauthorized");
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
  var isOffline = false;
  function setOffline(v) {
    if (v === isOffline) return;
    isOffline = v;
    offlineBanner.classList.toggle("show", v);
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
    appEl.classList.add("pairing-mode");
    document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
    $("view-pairing").classList.add("active");
    $("pairing-error").textContent = errorMsg || "";
    stopLeadStream();
    stopPulsePolling();
  }

  function showApp() {
    appEl.classList.remove("pairing-mode");
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
  // Projects
  // ---------------------------------------------------------------

  function loadProjects() {
    var list = $("projects-list");
    list.innerHTML = '<div class="empty-state">กำลังโหลด…</div>';
    apiFetch("api/projects")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var names = Array.isArray(data) ? data : (Array.isArray(data && data.projects) ? data.projects : []);
        renderProjects(names);
      })
      .catch(function () {
        list.innerHTML = '<div class="empty-state">โหลด projects ไม่สำเร็จ<br>ลองใหม่อีกครั้ง</div>';
      });
  }

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
      row.className = "project-row";
      var label = document.createElement("span");
      label.className = "name";
      label.textContent = name;
      row.appendChild(label);
      if (active) {
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "active";
        row.appendChild(tag);
      }
      row.addEventListener("click", function () { openProject(name); });
      list.appendChild(row);
    });
  }

  function openProject(name) {
    apiFetch("api/project/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    })
      .then(function (res) {
        if (res.status === 403) {
          toast("ต้องเปิด control mode ก่อนถึงจะสลับ project ได้");
          return;
        }
        if (!res.ok) {
          toast("เปิด project ไม่สำเร็จ");
          return;
        }
        toast("เปิด " + name);
      })
      .catch(function () { toast("เปิด project ไม่สำเร็จ"); });
  }

  // ---------------------------------------------------------------
  // Lead console (SSE)
  // ---------------------------------------------------------------

  var MAX_ES_RETRIES = 5;

  function appendMsg(kind, text) {
    if (typeof text !== "string" || !text) return;
    var log = $("lead-log");
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

  function connectSse(ticket) {
    var url = apiUrl("api/lead?ticket=" + encodeURIComponent(ticket));
    var es = new EventSource(url);
    state.es = es;
    es.onopen = function () {
      state.esRetries = 0;
    };
    es.onmessage = function (evt) {
      var text = null;
      try {
        var payload = JSON.parse(evt.data);
        // data-min: only ever surface the Lead's own text field.
        text = typeof payload === "object" && payload ? (payload.text || payload.message) : null;
      } catch (e) {
        text = evt.data;
      }
      appendMsg("lead", typeof text === "string" ? text : null);
    };
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
    $("view-control-note").textContent =
      state.mode === "control" ? "" : "โหมด view — ส่งข้อความไม่ได้จนกว่าจะเปิด control";
    $("lead-send").disabled = state.mode !== "control";
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
    $("pulse-total").textContent = "/ " + total + " total";
    var pct = total > 0 ? Math.min(100, Math.round((working / total) * 100)) : 0;
    $("pulse-ring").style.background =
      "conic-gradient(var(--accent) " + (pct * 3.6) + "deg, var(--line) 0deg)";
  }

  // ---------------------------------------------------------------
  // Mode discovery (view vs control) — best-effort from /api/projects response
  // or a dedicated field if the backend ever adds one; defaults to view.
  // ---------------------------------------------------------------

  function detectMode() {
    apiFetch("api/lead/say", { method: "OPTIONS" })
      .then(function (res) {
        state.mode = res.status === 403 ? "view" : "control";
        updateControlNote();
      })
      .catch(function () { /* stay in view mode assumption */ });
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
    detectMode();
  }

  init();
})();
