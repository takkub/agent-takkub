"use strict";

/*
 * Takkub Remote — vanilla JS SPA, no build step, no external requests.
 * Data-minimization contract (design doc §7.3): only ever render (1) the
 * Lead's own conversation/report text and (2) on Pulse, role name + provider
 * + runtime per project. Any other field the API might accidentally include (task
 * detail, pane id, transcript, state) is never read into the DOM.
 */
(function () {
  var LS_TOKEN = "takkub_remote_token";
  var LS_BASE = "takkub_remote_base";
  var LS_SESSION = "takkub_remote_session";

  var state = {
    token: localStorage.getItem(LS_TOKEN) || "",
    base: localStorage.getItem(LS_BASE) || "",
    session: localStorage.getItem(LS_SESSION) || "",
    mode: "view",
    view: "lead",
    pulseTimer: null,
    lastToast: 0,
    activeProject: "",
    selectedProject: "",
    provider: "claude",
    providersByProject: {},
    leadByProject: {},
    leadWorking: false,
    openTabs: [],
    opening: null,
    closing: null,
    resumePending: false,
    resumeProject: "",
    backgroundedAt: 0,
  };

  // Keep several project-scoped SSE subscriptions alive at once. The server
  // caps total SSE clients, so one phone intentionally leaves headroom for a
  // second device while still covering the common 2-4 open-project workflow.
  var MAX_PROJECT_STREAMS = 4;
  var MAX_IMAGE_BYTES = 8 * 1024 * 1024;

  function visibleProject() {
    return state.selectedProject || state.activeProject || "";
  }

  function projectLeadState(project) {
    if (!project) return null;
    if (!state.leadByProject[project]) {
      state.leadByProject[project] = {
        messages: [],
        historyLoaded: false,
        historyLoading: null,
        historyGeneration: 0,
        es: null,
        esRetries: 0,
        esTimer: null,
        ticketPending: false,
        transportGeneration: 0,
        connected: false,
        working: false,
        workingConfirmed: false,
        optimisticWorkingTimer: null,
        workingCategory: null,
        picker: null,
        lastLeadAt: 0,
      };
    }
    return state.leadByProject[project];
  }

  function clearOptimisticWorkingTimer(lead) {
    if (lead && lead.optimisticWorkingTimer) {
      clearTimeout(lead.optimisticWorkingTimer);
      lead.optimisticWorkingTimer = null;
    }
  }

  function setProjectWorking(project, working, category, confirmed) {
    var lead = projectLeadState(project);
    if (!lead) return;
    if (!working || confirmed) clearOptimisticWorkingTimer(lead);
    lead.working = !!working;
    lead.workingConfirmed = !!working && !!confirmed;
    lead.workingCategory = working ? (category || null) : null;
    if (project === visibleProject()) {
      state.leadWorking = lead.working;
      if (lead.working) showThinking(lead.workingCategory);
      else hideThinking();
    }
  }

  function beginOptimisticWorking(project) {
    var lead = projectLeadState(project);
    if (!lead) return;
    setProjectWorking(project, true, null, false);
    lead.optimisticWorkingTimer = setTimeout(function () {
      lead.optimisticWorkingTimer = null;
      if (!lead.workingConfirmed) setProjectWorking(project, false, null, false);
    }, 30000);
  }

  var VIEW_LABELS = { projects: "Projects", pulse: "Pulse" };

  // Self-contained brand marks keep the PWA's no-external-requests promise.
  // `codex` is accepted as the internal engine alias; the UI's public provider
  // name is OpenAI. Unknown/missing values deliberately fall back to Claude so the
  // frontend remains compatible with pre-multi-provider cockpit versions.
  var PROVIDERS = {
    claude: { name: "Claude", logo: "🧠", color: "#d97757" },
    openai: { name: "OpenAI", logo: "🤖", color: "#10a37f" },
    gemini: { name: "Gemini", logo: "✨", color: "#4285f4" },
    opencode: { name: "OpenCode", logo: "⌘", color: "#8b5cf6" },
    kimi: { name: "Kimi", logo: "🌙", color: "#6366f1" },
    cursor: { name: "Cursor", logo: "◈", color: "#22c55e" },
  };

  function normalizeProvider(value) {
    var name = typeof value === "string" ? value.trim().toLowerCase() : "";
    if (name === "codex") name = "openai";
    return PROVIDERS[name] ? name : "claude";
  }

  function providerMeta(value) {
    return PROVIDERS[normalizeProvider(value)];
  }

  function providerIdentity(value) {
    var meta = providerMeta(value);
    return meta.logo + " " + meta.name;
  }

  function updateProviderUI() {
    var meta = providerMeta(state.provider);
    document.documentElement.style.setProperty("--lead", meta.color);
    var icon = $("nav-lead-icon");
    var textEl = $("nav-lead-text");
    var navButton = icon && icon.closest("button");
    if (icon) icon.textContent = meta.logo;
    if (textEl) textEl.textContent = meta.name;
    if (navButton) navButton.setAttribute("aria-label", meta.name);
    updateHeaderTitle();
    updateControlNote();
  }

  function setProvider(value, project) {
    var fallback = (project && state.providersByProject[project]) || "claude";
    var provider = normalizeProvider(value || fallback);
    if (project) state.providersByProject[project] = provider;
    var visible = visibleProject();
    if (project && visible && project !== visible) return;
    if (state.provider === provider) return;
    state.provider = provider;
    updateProviderUI();
  }

  var VIEW_SUBTITLES = {
    projects: "เฉพาะที่ import ไว้",
    lead: "", // dynamic based on activeProject
    pulse: "เห็นแค่จำนวน"
  };

  function updateHeaderTitle() {
    var label = state.view === "lead"
      ? providerIdentity(state.provider)
      : (VIEW_LABELS[state.view] || "Takkub Remote");
    $("header-title").textContent = label;
    
    var sub = VIEW_SUBTITLES[state.view] || "";
    if (state.view === "lead" && (state.selectedProject || state.activeProject)) {
      sub = state.selectedProject || state.activeProject;
    }
    $("header-subtitle").textContent = sub;
  }

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
    if (state.session) headers["X-Session"] = state.session;
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
    state.session = "";
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_SESSION);
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
    updateHeaderTitle();
    if (name === "projects") loadProjects();
    if (name === "lead") {
      renderSelectedProject();
      startLeadStream();
    }
    if (name === "pulse") startPulsePolling();
    else stopPulsePolling();
    updateLeadActionVisibility();
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
          return r.json().then(function (data) {
            if (data && data.ok && typeof data.session === "string" && data.session) {
              state.session = data.session;
              localStorage.setItem(LS_SESSION, state.session);
            }
            // Clear any pre-auth retry state (e.g. an esTimer backoff from a
            // 403'd sse-ticket request before we had a session) so the
            // fresh startLeadStream() triggered by showApp() below isn't
            // guarded out — see historyLoaded decoupling in startLeadStream.
            stopLeadStream();
            enterAuthenticatedApp();
          });
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
        state.openTabs = Array.isArray(data && data.open_tabs) ? data.open_tabs : [];
        var items = Array.isArray(data && data.projects) ? data.projects : [];
        for (var i = 0; i < items.length; i++) {
          var item = items[i];
          var name = typeof item === "string" ? item : (item && item.name) || "";
          var active = typeof item === "object" && !!item.active;
          if (active && name) {
            state.activeProject = name;
            if (!state.selectedProject) state.selectedProject = name;
            updateHeaderTitle();
            break;
          }
        }
        syncProjectStreams();
        return items;
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

  // Open-tab projects are tappable — switch selectedProject + jump to Lead.
  // Imported-but-not-open projects are tappable too in control mode (taps
  // POST /api/open to spawn the Lead pane); read-only in view mode.
  function createProjectRow(proj) {
    var name = proj.name;
    var active = proj.active;
    var open = proj.open;
    var selected = proj.selected;
    var canOpen = !open && state.mode === "control";
    var row = document.createElement("div");
    row.className = "project-row" +
      (active ? " active" : "") +
      (selected ? " selected" : "") +
      (open || canOpen ? " tappable" : " readonly");

    var iconBox = document.createElement("span");
    iconBox.className = "icon-box";
    iconBox.textContent = active ? "📂" : "📁";
    row.appendChild(iconBox);

    var nameCol = document.createElement("div");
    nameCol.className = "name-col";

    var nameSpan = document.createElement("span");
    nameSpan.className = "name";
    nameSpan.textContent = name;
    nameCol.appendChild(nameSpan);

    if (proj.path) {
      var pathSpan = document.createElement("span");
      pathSpan.className = "hint";
      pathSpan.textContent = proj.path;
      nameCol.appendChild(pathSpan);
    }
    row.appendChild(nameCol);

    if (active) {
      var check = document.createElement("span");
      check.className = "ck";
      check.textContent = "✓";
      row.appendChild(check);
    }

    if (open) {
      row.addEventListener("click", function () { selectProject(name); });
      if (state.mode === "control") {
        var closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "close-btn";
        closeBtn.textContent = "✕";
        closeBtn.setAttribute("aria-label", "ปิด " + name);
        closeBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          closeProject(name, row);
        });
        row.appendChild(closeBtn);
      }
    } else if (canOpen) {
      row.addEventListener("click", function () { openProject(name, row); });
    }

    return row;
  }

  // Opens an imported-but-not-yet-open project: spawns the Lead pane on the
  // desktop cockpit (~2-5s), then jumps the mobile view to the Lead console.
  function openProject(name, row) {
    if (state.opening) return; // debounce — one open in flight at a time
    state.opening = name;
    row.classList.add("opening");
    var iconBox = row.querySelector(".icon-box");
    var spinner = null;
    if (iconBox) {
      spinner = document.createElement("span");
      spinner.className = "spin-icon";
      iconBox.innerHTML = "";
      iconBox.appendChild(spinner);
    }
    var nameSpan = row.querySelector(".name");
    var origName = nameSpan ? nameSpan.textContent : name;
    if (nameSpan) nameSpan.textContent = origName + " · กำลังเปิด…";

    apiFetch("api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: name }),
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status === 200 && res.data && res.data.ok) {
          if (state.openTabs.indexOf(name) === -1) state.openTabs.push(name);
          fetchProjectsAndMode().catch(function () {}); // background refresh, best-effort
          selectProject(name);
          return;
        }
        var msg = "เปิดไม่สำเร็จ";
        if (res.status === 400) msg = "ไม่พบ project";
        else if (res.status === 403 && res.data && res.data.msg === "view mode: control is disabled") msg = "อยู่โหมด view เปิดไม่ได้";
        else if (res.status === 409) msg = "เปิดไม่ได้ (โฟลเดอร์หาย)";
        toast(msg);
      })
      .catch(function (err) {
        // password_required / unauthorized are already surfaced by apiFetch
        // (password prompt / pairing screen) — nothing more to toast.
        if (err instanceof Error && (err.message === "password_required" || err.message === "unauthorized")) return;
        toast("เปิดไม่สำเร็จ ลองใหม่");
      })
      .then(function () {
        state.opening = null;
        row.classList.remove("opening");
        if (iconBox) iconBox.textContent = iconFor(name);
        if (nameSpan) nameSpan.textContent = origName;
      });
  }

  // Closes an open project tab on the desktop cockpit — terminates Lead +
  // every teammate pane for it. Confirms on the phone first (the desktop
  // dialog is skipped server-side for remote-originated closes; see
  // MainWindow._close_project_tab(confirm=False)).
  function closeProject(name, row) {
    if (state.closing) return; // debounce — one close in flight at a time
    if (!window.confirm("ปิด '" + name + "'? Lead และทุก teammate pane ของ project นี้จะถูกปิด")) return;
    state.closing = name;
    row.classList.add("closing");

    apiFetch("api/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: name }),
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status === 200 && res.data && res.data.ok) {
          state.openTabs = (state.openTabs || []).filter(function (n) { return n !== name; });
          stopLeadStream(name, true);
          if (state.selectedProject === name) state.selectedProject = null;
          fetchProjectsAndMode().then(renderProjects).catch(function () {});
          loadProjects();
          return;
        }
        var msg = "ปิดไม่สำเร็จ";
        if (res.status === 400) msg = "ไม่พบ project";
        else if (res.status === 403 && res.data && res.data.msg === "view mode: control is disabled") msg = "อยู่โหมด view ปิดไม่ได้";
        else if (res.status === 409) msg = "ปิดไม่สำเร็จ ลองใหม่อีกครั้ง";
        toast(msg);
      })
      .catch(function (err) {
        if (err instanceof Error && (err.message === "password_required" || err.message === "unauthorized")) return;
        toast("ปิดไม่สำเร็จ ลองใหม่");
      })
      .then(function () {
        state.closing = null;
        row.classList.remove("closing");
      });
  }

  function iconFor(name) {
    return name === state.activeProject ? "📂" : "📁";
  }

  // Switch the visible conversation only. Every project keeps its own
  // history cache + SSE subscription, so this is a local render operation —
  // no stop/start and no history API round-trip when the stream is warm.
  function selectProject(name) {
    if (!name) return;
    if (name === state.selectedProject) {
      switchView("lead");
      return;
    }
    state.selectedProject = name;
    setProvider(state.providersByProject[name] || "claude", name);
    updateHeaderTitle();
    renderSelectedProject();
    syncProjectStreams();
    switchView("lead");
    fetchPulse();
  }

  function renderProjects(items) {
    var list = $("projects-list");
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">ยังไม่มี project ที่ import ไว้</div>';
      return;
    }

    var openTabs = state.openTabs || [];
    var openItems = [];
    var importedItems = [];
    items.forEach(function (item) {
      var name = typeof item === "string" ? item : (item && item.name) || "";
      if (!name) return;
      var active = typeof item === "object" && !!item.active;
      var path = (typeof item === "object" && typeof item.path === "string") ? item.path : "";
      var isOpen = openTabs.indexOf(name) !== -1;
      var projObj = { name: name, active: active, path: path, open: isOpen, selected: name === state.selectedProject };
      if (isOpen) {
        openItems.push(projObj);
      } else {
        importedItems.push(projObj);
      }
    });

    if (openItems.length > 0) {
      var sectActive = document.createElement("div");
      sectActive.className = "sect";
      sectActive.textContent = "เปิดอยู่";
      list.appendChild(sectActive);

      openItems.forEach(function (proj) {
        list.appendChild(createProjectRow(proj));
      });
    }

    if (importedItems.length > 0) {
      var sectImported = document.createElement("div");
      sectImported.className = "sect";
      sectImported.style.marginTop = "14px";
      sectImported.textContent = "import ไว้";
      list.appendChild(sectImported);

      importedItems.forEach(function (proj) {
        list.appendChild(createProjectRow(proj));
      });
    }

    var note = document.createElement("p");
    note.className = "newnote";
    if (state.mode === "control") {
      note.textContent = importedItems.length > 0
        ? "แตะ project ที่ import ไว้เพื่อเปิด · แตะ project ที่เปิดอยู่เพื่อสลับดู Lead"
        : "แตะ project ที่เปิดอยู่เพื่อสลับดู Lead";
    } else {
      note.textContent = "อ่านอย่างเดียว · เปิด project ได้จาก cockpit บนเดสก์ท็อป หรือเปิด control mode ก่อน";
    }
    list.appendChild(note);
  }

  // ---------------------------------------------------------------
  // Lead console (SSE)
  // ---------------------------------------------------------------

  var ES_RETRY_WARNING_THRESHOLD = 5;
  var lastMsgKind = null;

  function timeLabel() {
    var d = new Date();
    var hh = String(d.getHours()).padStart(2, "0");
    var mm = String(d.getMinutes()).padStart(2, "0");
    return hh + ":" + mm;
  }

  // XSS-safe HTML escape: route every raw string through the DOM's own
  // textContent→innerHTML conversion, never a hand-rolled regex.
  function mdEscape(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // Inline markdown (bold/italic/strike/code/links/images) — always escapes first,
  // then applies patterns to the *escaped* string so a `<script>` or `**` in
  // user/Lead text can never become live markup.
  function mdInline(raw) {
    var s = mdEscape(raw);
    // Render only embedded raster data URLs. External URLs would disclose the
    // phone's IP/referrer to arbitrary hosts, while SVG data can execute active
    // content in some browser contexts. The strict allow-list keeps images
    // self-contained and compatible with the PWA's CSP.
    s = s.replace(/!\[([^\]\n]{0,200})\]\((data:image\/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+)\)/g, function (m, alt, src) {
      return '<img class="remote-image" src="' + src + '" alt="' + alt + '" loading="lazy" decoding="async">';
    });
    s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, function (m, t, u) {
      // mdEscape only escapes &/</> (safe for text nodes); a literal " here would
      // break out of the href attribute, so quote-escape separately for this context.
      var safeHref = u.replace(/"/g, "&quot;");
      return '<a href="' + safeHref + '" target="_blank" rel="noopener noreferrer">' + t + "</a>";
    });
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    return s;
  }

  // Block-level markdown → HTML. Vanilla, line-based (no external lib — CSP
  // blocks CDN). Supports #/##/### headers, fenced code, tables, lists,
  // blockquotes, hr, paragraphs. Every leaf goes through mdInline/mdEscape,
  // so this is XSS-safe by construction — never an innerHTML of raw text.
  function renderMarkdown(text) {
    if (typeof text !== "string" || !text) return "";
    var lines = text.replace(/\r\n/g, "\n").split("\n");
    var n = lines.length;
    var i = 0;
    var html = "";

    var reFence = /^```(\w*)\s*$/;
    var reHeader = /^(#{1,3})\s+(.*)$/;
    var reHr = /^(-{3,}|\*{3,}|_{3,})\s*$/;
    var reQuote = /^>\s?/;
    var reUl = /^\s*[-*]\s+(.*)$/;
    var reOl = /^\s*\d+\.\s+(.*)$/;
    var reTableRow = /^\s*\|.*\|\s*$/;
    var reTableSep = /^\s*\|?[\s:|-]+\|?\s*$/;

    while (i < n) {
      var line = lines[i];

      if (reFence.test(line)) {
        var codeLines = [];
        i++;
        while (i < n && !/^```\s*$/.test(lines[i])) { codeLines.push(lines[i]); i++; }
        i++; // closing fence
        html += "<pre><code>" + mdEscape(codeLines.join("\n")) + "</code></pre>";
        continue;
      }

      if (/^\s*$/.test(line)) { i++; continue; }

      var h = reHeader.exec(line);
      if (h) {
        var level = h[1].length;
        html += "<h" + level + ">" + mdInline(h[2]) + "</h" + level + ">";
        i++;
        continue;
      }

      if (reHr.test(line)) { html += "<hr>"; i++; continue; }

      if (reQuote.test(line)) {
        var quoteLines = [];
        while (i < n && reQuote.test(lines[i])) { quoteLines.push(lines[i].replace(reQuote, "")); i++; }
        html += "<blockquote>" + mdInline(quoteLines.join(" ")) + "</blockquote>";
        continue;
      }

      if (reTableRow.test(line) && i + 1 < n && reTableSep.test(lines[i + 1]) && /-/.test(lines[i + 1])) {
        var splitRow = function (row) {
          return row.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
        };
        var headCells = splitRow(line);
        i += 2;
        var bodyRows = [];
        while (i < n && reTableRow.test(lines[i])) { bodyRows.push(splitRow(lines[i])); i++; }
        var tbl = '<div class="tbl-wrap"><table><thead><tr>';
        headCells.forEach(function (c) { tbl += "<th>" + mdInline(c) + "</th>"; });
        tbl += "</tr></thead><tbody>";
        bodyRows.forEach(function (r) {
          tbl += "<tr>";
          r.forEach(function (c) { tbl += "<td>" + mdInline(c) + "</td>"; });
          tbl += "</tr>";
        });
        tbl += "</tbody></table></div>";
        html += tbl;
        continue;
      }

      var ulMatch = reUl.exec(line);
      if (ulMatch) {
        var uItems = [];
        while (i < n && (ulMatch = reUl.exec(lines[i]))) { uItems.push(ulMatch[1]); i++; }
        html += "<ul>" + uItems.map(function (it) { return "<li>" + mdInline(it) + "</li>"; }).join("") + "</ul>";
        continue;
      }

      var olMatch = reOl.exec(line);
      if (olMatch) {
        var oItems = [];
        while (i < n && (olMatch = reOl.exec(lines[i]))) { oItems.push(olMatch[1]); i++; }
        html += "<ol>" + oItems.map(function (it) { return "<li>" + mdInline(it) + "</li>"; }).join("") + "</ol>";
        continue;
      }

      var para = [];
      while (
        i < n && lines[i].trim() !== "" &&
        !reFence.test(lines[i]) && !reHeader.test(lines[i]) && !reQuote.test(lines[i]) &&
        !reUl.test(lines[i]) && !reOl.test(lines[i]) && !reHr.test(lines[i]) && !reTableRow.test(lines[i])
      ) { para.push(lines[i]); i++; }
      if (para.length) {
        html += "<p>" + mdInline(para.join("\n")).replace(/\n/g, "<br>") + "</p>";
      } else {
        html += "<p>" + mdInline(lines[i]) + "</p>";
        i++;
      }
    }

    return html;
  }

  var THINKING_LABELS = {
    reading: "📖 กำลังอ่านไฟล์…",
    editing: "✏️ กำลังแก้ไฟล์…",
    running: "⚙️ กำลังรันคำสั่ง…",
    web: "🌐 กำลังค้นเว็บ…",
    delegating: "👥 กำลังมอบงานทีม…",
    skill: "🛠️ กำลังใช้ skill…",
  };
  function thinkingDefaultLabel() {
    return "⏳ " + providerMeta(state.provider).name + " กำลังทำงาน…";
  }
  function thinkingLabelFor(category) {
    return (category && THINKING_LABELS[category]) || thinkingDefaultLabel();
  }

  var thinkingEl = null;
  function showThinking(category) {
    var label = thinkingLabelFor(category);
    if (thinkingEl) {
      // already showing — just update the label text for the latest category
      var labelEl = thinkingEl.querySelector(".thinking-label");
      if (labelEl) labelEl.textContent = label;
      return;
    }
    var log = $("lead-log");
    var emptyEl = $("lead-empty");
    if (emptyEl) emptyEl.remove();
    var div = document.createElement("div");
    div.className = "msg lead thinking group-start";
    var who = document.createElement("div");
    who.className = "who";
    var dot = document.createElement("span");
    dot.className = "dot";
    who.appendChild(dot);
    who.appendChild(document.createTextNode(providerIdentity(state.provider)));
    div.appendChild(who);
    var body = document.createElement("div");
    body.className = "msg-body";
    var labelSpan = document.createElement("span");
    labelSpan.className = "thinking-label";
    labelSpan.textContent = label;
    body.appendChild(labelSpan);
    body.insertAdjacentHTML("beforeend", ' <span class="thinking-dots"><span></span><span></span><span></span></span>');
    div.appendChild(body);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    thinkingEl = div;
    // Do NOT reset lastMsgKind/lastLeadBodyEl here: a 'working' SSE event
    // fires between consecutive 'lead' chunks of the same reply (tool_use
    // in between), and appendMsg()'s hideThinking() already tore down the
    // previous thinkingEl, so every such event used to land here and reset
    // lastMsgKind — breaking appendLeadLive's merge window (Bug C: two SSE
    // 'lead' events <4s apart always rendered as separate bubbles instead
    // of merging). appendLeadLive's own time-window + kind check is enough
    // to decide whether to merge or start a new group.
  }
  function hideThinking() {
    if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  }

  function appendMsgDom(kind, text) {
    if (typeof text !== "string" || !text) return;
    hideThinking();
    var log = $("lead-log");
    var emptyEl = $("lead-empty");
    if (emptyEl) emptyEl.remove();
    var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    var isGroupStart = kind !== lastMsgKind;
    lastMsgKind = kind;
    var isOk = kind === "lead" && text.indexOf("✅") >= 0;

    // A 'sys' message is meant to be a short one-liner pill; if a caller ever
    // hands it long or multi-line text, fall back to the wrapped card style
    // instead of letting it stretch into an unreadable oval.
    var isSysWrap = kind === "sys" && (text.length > 60 || text.indexOf("\n") >= 0);

    var div = document.createElement("div");
    div.className = "msg " + kind + (isGroupStart ? " group-start" : "") + (isOk ? " ok" : "") + (isSysWrap ? " sys-wrap" : "");

    if (kind === "lead" && isGroupStart) {
      var who = document.createElement("div");
      who.className = "who";
      var dot = document.createElement("span");
      dot.className = "dot";
      who.appendChild(dot);
      who.appendChild(document.createTextNode(providerIdentity(state.provider)));
      var time = document.createElement("span");
      time.className = "time";
      time.textContent = timeLabel();
      who.appendChild(time);
      div.appendChild(who);
    } else if (kind === "done") {
      var doneChip = document.createElement("div");
      doneChip.className = "who done-chip";
      doneChip.appendChild(document.createTextNode("✅ done"));
      var doneTime = document.createElement("span");
      doneTime.className = "time";
      doneTime.textContent = timeLabel();
      doneChip.appendChild(doneTime);
      div.appendChild(doneChip);
    }

    var body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = kind === "lead" ? renderMarkdown(text) : mdInline(text);
    div.appendChild(body);

    log.appendChild(div);
    if (atBottom) log.scrollTop = log.scrollHeight;
    if (kind === "lead") {
      lastLeadRawAccum = text;
      hidePickerBanner();
      renderQuickReplies();
    }
    // Keep the "…" alive *below* the message we just added whenever the Lead
    // is still working — a text block mid-turn must not read as "done".
    if (state.leadWorking) showThinking();
  }

  function appendProjectMessage(project, kind, text) {
    if (typeof text !== "string" || !text || !project) return;
    var lead = projectLeadState(project);
    if (kind === "lead") lead.picker = null;
    lead.messages.push({ kind: kind, text: text });
    if (project === visibleProject()) appendMsgDom(kind, text);
  }

  // Existing composer/quick-reply callers target the currently visible
  // project. SSE handlers use appendProjectMessage with their captured
  // project namespace so background streams never paint into the wrong tab.
  function appendMsg(kind, text) {
    appendProjectMessage(visibleProject(), kind, text);
  }

  // Live SSE 'lead' events land one-per-backend-record (notify.py pushes
  // each assistant record separately), which fragments a single Lead reply
  // into several stacked bubbles. Root cause is server-side (out of scope
  // here) — this only smooths the *rendering*: consecutive live events
  // within LEAD_MERGE_WINDOW_MS fold into the previous bubble's body instead
  // of stacking a new one. History replay (loadHistory) never merges —
  // there's no timestamp on stored entries, so kind-adjacency alone can't
  // tell "same reply, chunked" from "two separate replies, re-rendered".
  var LEAD_MERGE_WINDOW_MS = 4000;
  var lastLeadBodyEl = null;
  var lastLeadAt = 0;
  // Accumulated raw text of the *current* Lead reply group (reset each new
  // group, appended on merge) — quick-reply numbered-option detection reads
  // this rather than the rendered DOM.
  var lastLeadRawAccum = "";

  function appendLeadLive(text, project) {
    project = project || visibleProject();
    if (typeof text !== "string" || !text || !project) return;
    var leadState = projectLeadState(project);
    leadState.picker = null;
    var now = Date.now();
    var prior = leadState.messages.length
      ? leadState.messages[leadState.messages.length - 1]
      : null;
    var shouldMerge = prior && prior.kind === "lead" &&
      (now - leadState.lastLeadAt) <= LEAD_MERGE_WINDOW_MS;
    if (shouldMerge) {
      prior.text += "\n" + text;
      leadState.lastLeadAt = now;
    } else {
      leadState.messages.push({ kind: "lead", text: text });
      leadState.lastLeadAt = now;
    }
    if (project !== visibleProject()) return;
    hidePickerBanner();
    if (shouldMerge && lastLeadBodyEl && lastMsgKind === "lead") {
      hideThinking();
      var log = $("lead-log");
      var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
      lastLeadBodyEl.insertAdjacentHTML("beforeend", renderMarkdown(text));
      lastLeadAt = now;
      lastLeadRawAccum += "\n" + text;
      renderQuickReplies();
      if (atBottom) log.scrollTop = log.scrollHeight;
      if (state.leadWorking) showThinking();
      return;
    }
    appendMsgDom("lead", text);
    lastLeadAt = now;
    var bodies = document.querySelectorAll("#lead-log .msg.lead .msg-body");
    lastLeadBodyEl = bodies.length ? bodies[bodies.length - 1] : null;
  }

  function ensureLeadEmpty(text) {
    var log = $("lead-log");
    if (!log) return;
    var empty = $("lead-empty");
    if (!empty) {
      empty = document.createElement("div");
      empty.id = "lead-empty";
      var icon = document.createElement("span");
      icon.className = "icon";
      icon.textContent = "💬";
      var label = document.createElement("span");
      label.className = "text";
      empty.appendChild(icon);
      empty.appendChild(label);
      log.appendChild(empty);
    }
    var txt = empty.querySelector(".text");
    if (txt) txt.textContent = text;
  }

  function renderSelectedProject() {
    var project = visibleProject();
    var lead = projectLeadState(project);
    var log = $("lead-log");
    if (!lead || !log) return;
    hideThinking();
    hidePickerBanner();
    var old = log.querySelectorAll(".msg, #lead-empty");
    for (var i = 0; i < old.length; i++) old[i].remove();
    lastMsgKind = null;
    lastLeadBodyEl = null;
    lastLeadAt = 0;
    lastLeadRawAccum = "";
    var isWorking = !!lead.working;
    state.leadWorking = false;
    lead.messages.forEach(function (message) {
      appendMsgDom(message.kind, message.text);
    });
    state.leadWorking = isWorking;
    if (!lead.messages.length && !isWorking) {
      ensureLeadEmpty(
        lead.historyLoaded
          ? "ยังไม่มีข้อความ — พิมพ์ถึง " + providerMeta(state.provider).name + " ด้านล่างเพื่อเริ่ม"
          : "กำลังเชื่อมต่อ…"
      );
    }
    if (isWorking) showThinking(lead.workingCategory);
    if (lead.picker) showPickerBanner(lead.picker);
    var last = lead.messages.length ? lead.messages[lead.messages.length - 1] : null;
    if (last && last.kind === "lead") {
      var bodies = document.querySelectorAll("#lead-log .msg.lead .msg-body");
      lastLeadBodyEl = bodies.length ? bodies[bodies.length - 1] : null;
      lastLeadAt = lead.lastLeadAt;
    }
    renderQuickReplies();
  }

  // ---------------------------------------------------------------
  // Quick-reply chips (W2a MVP) + AskUserQuestion picker fallback banner
  // ---------------------------------------------------------------

  var STANDARD_QUICK_REPLIES = ["ok ลุยเลย", "ไม่เอา หยุดก่อน", "ขอดูแผนก่อน"];

  // Numbered-option auto-detect: matches "1. ...", "2) ...", "ข้อ 3 ..." at
  // the start of a line in the Lead's latest reply text, up to 6 distinct
  // numbers, in first-seen order.
  function detectNumberedOptions(text) {
    if (typeof text !== "string" || !text) return [];
    var re = /(?:^|\n)\s*(?:ข้อ\s*)?(\d{1,2})[.\)]\s+/g;
    var nums = [];
    var m;
    while ((m = re.exec(text)) && nums.length < 6) {
      if (nums.indexOf(m[1]) === -1) nums.push(m[1]);
    }
    return nums;
  }

  function makeQuickChip(label, isNum, onClick) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "qr-chip" + (isNum ? " qr-num" : "");
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    return btn;
  }

  function renderQuickReplies() {
    var wrap = $("quick-replies");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (state.mode !== "control") {
      wrap.classList.remove("show");
      return;
    }
    wrap.classList.add("show");
    detectNumberedOptions(lastLeadRawAccum).forEach(function (n) {
      wrap.appendChild(makeQuickChip(n, true, function () { sendLeadMessage(n); }));
    });
    STANDARD_QUICK_REPLIES.forEach(function (label) {
      wrap.appendChild(makeQuickChip(label, false, function () { sendLeadMessage(label); }));
    });
  }

  // B2: renders tappable option chips from the provider-neutral picker
  // payload emitted by notify.py instead of forcing the phone user to answer
  // on the desktop. Providers without structured options use the same plain
  // "answer on desktop" fallback below.
  function showPickerBanner(payload) {
    var el = $("lead-picker-banner");
    if (!el) return;
    el.innerHTML = "";
    var prompt = "";
    var options = [];
    var multiSelect = false;
    if (payload && typeof payload === "object") {
      prompt = typeof payload.prompt === "string" ? payload.prompt : "";
      if (Array.isArray(payload.options)) options = payload.options;
      multiSelect = !!payload.multiSelect;
    } else if (typeof payload === "string") {
      prompt = payload;
    }
    var isControl = state.mode === "control";
    var main = document.createElement("span");
    main.textContent = options.length
      ? (isControl ? "⏸️ Lead ถามคำถาม — แตะเพื่อตอบ" : "⏸️ Lead รอคำตอบ — เปิด control mode เพื่อตอบ")
      : "⏸️ Lead รอคำตอบจาก picker บน desktop — ตอบบนจอคอมก่อน";
    el.appendChild(main);
    if (prompt.trim()) {
      var q = document.createElement("span");
      q.className = "q";
      q.textContent = prompt.trim();
      el.appendChild(q);
    }
    if (options.length) {
      var chipsWrap = document.createElement("div");
      chipsWrap.className = "picker-chips";
      var selected = [];
      options.forEach(function (opt, i) {
        var label = String((opt && opt.label) || "");
        if (!label) return;
        var idx = opt && typeof opt.index === "number" ? opt.index : i;
        var chip = makeQuickChip((idx + 1) + ". " + label, false, function () {
          if (state.mode !== "control") return;
          if (multiSelect) {
            chip.classList.toggle("picked");
            var pos = selected.indexOf(label);
            if (pos === -1) selected.push(label); else selected.splice(pos, 1);
          } else {
            sendLeadMessage(label);
          }
        });
        if (!isControl) chip.disabled = true;
        chipsWrap.appendChild(chip);
      });
      el.appendChild(chipsWrap);
      if (multiSelect) {
        var confirmBtn = makeQuickChip("ยืนยัน", false, function () {
          if (state.mode !== "control" || !selected.length) return;
          sendLeadMessage(selected.join(", "));
        });
        confirmBtn.className += " picker-confirm";
        confirmBtn.disabled = !isControl;
        el.appendChild(confirmBtn);
      }
    }
    el.classList.add("show");
  }

  function hidePickerBanner() {
    var el = $("lead-picker-banner");
    if (el) el.classList.remove("show");
  }

  // Shared send path for both the composer submit and quick-reply chip taps.
  function sendLeadMessage(text) {
    if (state.mode !== "control") return;
    text = (text || "").trim();
    if (!text) return;
    var project = visibleProject();
    appendProjectMessage(project, "me", text);
    var targetLead = projectLeadState(project);
    if (targetLead) {
      targetLead.picker = null;
      beginOptimisticWorking(project);
    }
    showThinking();
    var sayBody = { text: text, project: project };
    apiFetch("api/lead/say", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sayBody),
    }).catch(function () {
      if (targetLead) setProjectWorking(project, false, null, false);
      toast("ส่งข้อความไม่สำเร็จ");
    });
  }

  function sendLeadImage(file) {
    if (state.mode !== "control" || !file) return;
    if (file.size > MAX_IMAGE_BYTES) {
      toast("รูปใหญ่เกิน 8 MB");
      return;
    }
    if (["image/png", "image/jpeg", "image/webp", "image/gif"].indexOf(file.type) === -1) {
      toast("รองรับ PNG, JPEG, WebP และ GIF");
      return;
    }
    var project = visibleProject();
    var input = $("lead-input");
    var caption = input.value.trim();
    var attach = $("lead-attach");
    attach.disabled = true;
    toast("กำลังส่งรูป…");
    var reader = new FileReader();
    reader.onerror = function () {
      attach.disabled = false;
      toast("อ่านรูปไม่สำเร็จ");
    };
    reader.onload = function () {
      var dataUrl = typeof reader.result === "string" ? reader.result : "";
      apiFetch("api/lead/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          data_url: dataUrl,
          name: file.name || "image",
          caption: caption,
          project: project,
        }),
      })
        .then(function (r) {
          return r.json().then(function (data) { return { status: r.status, data: data }; });
        })
        .then(function (res) {
          if (res.status !== 200 || !res.data || !res.data.ok) {
            throw new Error((res.data && res.data.msg) || "upload failed");
          }
          if (caption) {
            input.value = "";
            autosizeInput();
          }
          appendProjectMessage(
            project,
            "me",
            (caption ? caption + "\n" : "") + "![" + (file.name || "image") + "](" + dataUrl + ")"
          );
          var targetLead = projectLeadState(project);
          if (targetLead) {
            beginOptimisticWorking(project);
          }
          showThinking();
          toast("ส่งรูปแล้ว");
        })
        .catch(function (err) {
          if (err instanceof Error && (err.message === "password_required" || err.message === "unauthorized")) return;
          toast(err instanceof Error && err.message ? err.message : "ส่งรูปไม่สำเร็จ");
        })
        .then(function () {
          attach.disabled = state.mode !== "control";
        });
    };
    reader.readAsDataURL(file);
  }

  function setLeadEmptyText(text) {
    ensureLeadEmpty(text);
  }

  // Load each project's history exactly once per authenticated app lifetime.
  // Project switches render this cache and never repeat the request.
  function loadHistory(project, force) {
    var lead = projectLeadState(project);
    if (!lead) return Promise.resolve();
    if (force) {
      // Invalidate any older in-flight snapshot. Keep rendering the existing
      // cache until the fresh response lands, avoiding a blank/flickering UI.
      lead.historyGeneration += 1;
      lead.historyLoaded = false;
      lead.historyLoading = null;
    }
    if (lead.historyLoaded) return Promise.resolve();
    if (lead.historyLoading) return lead.historyLoading;
    var generation = lead.historyGeneration;
    var preserveFrom = force ? lead.messages.length : 0;
    var path = "api/lead/history?limit=200";
    path += "&project=" + encodeURIComponent(project);
    var refreshError = null;
    var request = apiFetch(path, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (state.leadByProject[project] !== lead || lead.historyGeneration !== generation) return;
        setProvider(data && data.provider, project);
        var messages = Array.isArray(data && data.messages) ? data.messages : [];
        // The composer can be used while history is still in flight. Preserve
        // those optimistic project-scoped messages instead of replacing them
        // when the older history snapshot arrives.
        var pending = lead.messages.slice(preserveFrom);
        lead.messages = [];
        messages.forEach(function (m) {
          var text = m && typeof m.text === "string" ? m.text : null;
          if (text) lead.messages.push({ kind: m && m.kind === "me" ? "me" : "lead", text: text });
        });
        pending.forEach(function (message) {
          var duplicate = lead.messages.slice(-10).some(function (stored) {
            return stored.kind === message.kind && stored.text === message.text;
          });
          if (!duplicate) lead.messages.push(message);
        });
        // A freshly opened phone (or a manual refresh) must converge on the
        // pane's real status even when it missed the working/idle SSE edge.
        // Do not clobber a message that this phone has just submitted while
        // the history request was already in flight.
        if (!lead.optimisticWorkingTimer) {
          setProjectWorking(project, !!(data && data.working), null, true);
        }
        lead.historyLoaded = true;
        if (project === visibleProject()) renderSelectedProject();
      })
      .catch(function (err) {
        if (state.leadByProject[project] !== lead || lead.historyGeneration !== generation) return;
        // Best-effort — a live stream is still useful when provider history
        // is unavailable. Mark the attempt complete so tab switches do not
        // hammer the same failed endpoint.
        lead.historyLoaded = true;
        refreshError = err;
        if (project === visibleProject()) renderSelectedProject();
      })
      .then(function () {
        if (state.leadByProject[project] === lead && lead.historyGeneration === generation) {
          lead.historyLoading = null;
        }
        if (force && refreshError) throw refreshError;
      });
    lead.historyLoading = request;
    return lead.historyLoading;
  }

  function refreshProjectHistory(project, announce) {
    if (!project) return Promise.resolve();
    var btn = $("lead-refresh-btn");
    if (announce && btn) btn.disabled = true;
    return loadHistory(project, true)
      .then(function () {
        if (announce) toast("โหลดข้อความล่าสุดแล้ว");
      })
      .catch(function () {
        if (announce) toast("โหลดข้อความล่าสุดไม่สำเร็จ");
      })
      .then(function () {
        if (announce && btn) btn.disabled = false;
      });
  }

  function refreshOpenProjectHistories() {
    var ordered = [];
    var selected = visibleProject();
    if (selected) ordered.push(selected);
    (state.openTabs || []).forEach(function (project) {
      if (project && ordered.indexOf(project) === -1) ordered.push(project);
    });
    return Promise.all(ordered.slice(0, MAX_PROJECT_STREAMS).map(function (project) {
      return refreshProjectHistory(project, false);
    }));
  }

  function startLeadStream(project) {
    updateControlNote();
    if (project) {
      startProjectStream(project);
      return;
    }
    syncProjectStreams();
  }

  function syncProjectStreams() {
    var ordered = [];
    var selected = visibleProject();
    if (selected) ordered.push(selected);
    (state.openTabs || []).forEach(function (project) {
      if (project && ordered.indexOf(project) === -1) ordered.push(project);
    });
    var wanted = ordered.slice(0, MAX_PROJECT_STREAMS);
    wanted.forEach(startProjectStream);
    Object.keys(state.leadByProject).forEach(function (project) {
      if (wanted.indexOf(project) === -1) stopProjectTransport(project);
    });
  }

  function startProjectStream(project) {
    var lead = projectLeadState(project);
    if (!lead || lead.es || lead.esTimer || lead.historyLoading || lead.ticketPending) return;
    if (!lead.historyLoaded) {
      if (project === visibleProject()) setLeadEmptyText("กำลังเชื่อมต่อ…");
      loadHistory(project).then(function () {
        if (state.leadByProject[project] === lead && !lead.es && !lead.esTimer) {
          requestTicketAndConnect(project);
        }
      });
      return;
    }
    requestTicketAndConnect(project);
  }

  function requestTicketAndConnect(project) {
    var lead = projectLeadState(project);
    if (!lead || lead.es || lead.ticketPending) return;
    lead.ticketPending = true;
    var generation = lead.transportGeneration;
    apiFetch("api/sse-ticket", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: project }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var ticket = data && data.ticket;
        if (!ticket) throw new Error("no ticket");
        if (state.leadByProject[project] !== lead || lead.transportGeneration !== generation) {
          return;
        }
        lead.ticketPending = false;
        connectSse(ticket, project);
      })
      .catch(function () {
        if (state.leadByProject[project] !== lead || lead.transportGeneration !== generation) {
          return;
        }
        lead.ticketPending = false;
        scheduleEsRetry(project);
      });
  }

  // data-min: only ever surface the Lead's own text field, whether the
  // server sends a bare string or a JSON-wrapped {text|message} payload.
  function parseSseData(raw, project) {
    try {
      var payload = JSON.parse(raw);
      if (typeof payload === "string") return payload;
      if (payload && typeof payload === "object") {
        if (payload.provider) {
          setProvider(payload.provider, project);
        }
        var text = payload.text || payload.message;
        return typeof text === "string" ? text : null;
      }
      return null;
    } catch (e) {
      return typeof raw === "string" ? raw : null;
    }
  }

  function connectSse(ticket, project) {
    var lead = projectLeadState(project);
    if (!lead) return;
    lead.transportGeneration += 1;
    var url = apiUrl("api/lead?ticket=" + encodeURIComponent(ticket));
    var es = new EventSource(url);
    lead.es = es;
    es.onopen = function () {
      lead.esRetries = 0;
      lead.connected = true;
      setOffline(false);
      // Preserve an in-progress turn across transport reconnects. The server
      // emits working/idle only on pane-state transitions, so reconnecting in
      // the middle of a turn may not receive another working edge.
      if (project === visibleProject()) renderSelectedProject();
    };
    // Backend sends 'working' whenever the Lead is actively doing something
    // (tool_use/thinking) with no reply text yet — payload is a JSON
    // {text: category} where category is one of reading/editing/running/
    // web/delegating/skill/working, mapped to a Thai label so the remote
    // shows *what* the Lead is doing, not just a bare "…".
    es.addEventListener("working", function (evt) {
      setProjectWorking(project, true, parseSseData(evt.data, project), true);
    });
    // Lead pane went idle (turn finished) — drop the "…" the instant the
    // desktop spinner stops, so the phone never shows a stale "working" state.
    es.addEventListener("idle", function () {
      setProjectWorking(project, false, null, true);
    });
    es.addEventListener("lead", function (evt) {
      // A very fast turn may produce reply text between notifier polls, with
      // no observable pane-state edge. Reply text is enough to retire only
      // the local optimistic spinner; a confirmed working state stays until
      // the authoritative idle event arrives.
      if (!lead.workingConfirmed) setProjectWorking(project, false, null, false);
      appendLeadLive(parseSseData(evt.data, project), project);
    });
    // Mirror prompts typed in the desktop Lead pane to every connected
    // phone. Messages originating from this same Remote client are already
    // painted optimistically, so suppress that one immediate echo only.
    es.addEventListener("user", function (evt) {
      var payload = null;
      try {
        payload = JSON.parse(evt.data);
      } catch (e) {
        payload = null;
      }
      var text = payload && typeof payload.text === "string"
        ? payload.text
        : parseSseData(evt.data, project);
      if (!text) return;
      var last = lead.messages.length ? lead.messages[lead.messages.length - 1] : null;
      if (payload && payload.remote && last && last.kind === "me" && last.text === text) return;
      appendProjectMessage(project, "me", text);
    });
    es.addEventListener("done", function (evt) {
      setProjectWorking(project, false, null, true);
      appendProjectMessage(project, "done", parseSseData(evt.data, project));
    });
    // A session was resumed/replaced on the desktop while this project's SSE
    // remained connected. Invalidate only this project's history request and
    // reload the newly resolved provider transcript; other projects keep
    // streaming untouched.
    es.addEventListener("session_changed", function (evt) {
      parseSseData(evt.data, project);
      setProjectWorking(project, false, null, true);
      lead.historyGeneration += 1;
      lead.historyLoading = null;
      lead.historyLoaded = false;
      lead.messages = [];
      lead.lastLeadAt = 0;
      if (project === visibleProject()) renderSelectedProject();
      loadHistory(project);
    });
    // W2a/B2: a real AskUserQuestion picker fired on the desktop — surface a
    // banner with tappable option chips instead of hanging silently; cleared
    // by the next 'lead' text event (appendMsg/appendLeadLive both call
    // hidePickerBanner). Payload is the structured
    // {prompt, options[], multiSelect} shape (server: notify.py), not the
    // generic {text} shape parseSseData understands, so it's parsed directly.
    es.addEventListener("blocked_on_picker", function (evt) {
      var payload = null;
      try {
        payload = JSON.parse(evt.data);
      } catch (e) {
        payload = null;
      }
      lead.picker = payload;
      if (project === visibleProject()) showPickerBanner(payload);
    });
    es.onerror = function () {
      es.close();
      lead.es = null;
      lead.connected = false;
      if (project === visibleProject() && !lead.messages.length) {
        setLeadEmptyText("การเชื่อมต่อขัดข้อง — กำลังพยายามเชื่อมต่อใหม่…");
      }
      scheduleEsRetry(project);
    };
  }

  function scheduleEsRetry(project) {
    var lead = projectLeadState(project);
    if (!lead || lead.esTimer) return;
    lead.esRetries += 1;
    if (lead.esRetries === ES_RETRY_WARNING_THRESHOLD + 1) {
      if (project === visibleProject()) {
        setLeadEmptyText("การเชื่อมต่อยังไม่กลับมา — ระบบจะลองใหม่อัตโนมัติ…");
      }
      appendProjectMessage(
        project,
        "sys",
        "การเชื่อมต่อ " + providerMeta(state.providersByProject[project]).name + " ขาดช่วง — กำลังลองใหม่อัตโนมัติ"
      );
    }
    // Never give up after a fixed retry count. A phone can spend minutes on
    // another network or in the background and should heal when it returns.
    var delay = Math.min(1000 * Math.pow(2, lead.esRetries), 15000);
    lead.esTimer = setTimeout(function () {
      lead.esTimer = null;
      requestTicketAndConnect(project);
    }, delay);
  }

  function stopProjectTransport(project) {
    var lead = state.leadByProject[project];
    if (!lead) return;
    // Invalidate an in-flight ticket request before clearing its marker.  A
    // late response must never resurrect a stream that was intentionally
    // stopped because its tab closed or fell outside the background limit.
    lead.transportGeneration += 1;
    if (lead.es) { lead.es.close(); lead.es = null; }
    if (lead.esTimer) { clearTimeout(lead.esTimer); lead.esTimer = null; }
    lead.ticketPending = false;
    lead.connected = false;
    lead.esRetries = 0;
  }

  function stopLeadStream(project, discard) {
    if (project) {
      stopProjectTransport(project);
      if (discard) {
        clearOptimisticWorkingTimer(state.leadByProject[project]);
        delete state.leadByProject[project];
      }
    } else {
      Object.keys(state.leadByProject).forEach(stopProjectTransport);
    }
    if (!project || project === visibleProject()) {
      state.leadWorking = false;
      hideThinking();
      hidePickerBanner();
    }
  }

  function updateControlNote() {
    var isControl = state.mode === "control";
    $("view-control-note").textContent = isControl
      ? ""
      : "โหมด view — ส่งข้อความไม่ได้ · เปิด control ได้จาก cockpit บนเดสก์ท็อป";
    $("lead-send").disabled = !isControl;
    $("lead-attach").disabled = !isControl;
    var input = $("lead-input");
    input.disabled = !isControl;
    input.placeholder = isControl
      ? "พิมพ์ถึง " + providerMeta(state.provider).name + "…"
      : "อ่านอย่างเดียว (view mode)";
    var modePill = $("status-mode");
    modePill.textContent = isControl ? "CONTROL" : "VIEW";
    modePill.classList.toggle("control", isControl);
    renderQuickReplies();
    // Picker chips (B2) were rendered with isControl baked in at showPickerBanner
    // time — if the mode toggles while a banner is still showing (no new picker
    // event to re-render it), flip their disabled state directly instead of
    // leaving stale-enabled chips a view-mode user could still tap.
    document.querySelectorAll("#lead-picker-banner button").forEach(function (btn) {
      btn.disabled = !isControl;
    });
    updateLeadActionVisibility();
  }

  // ---------------------------------------------------------------
  // Resume / session picker (W3) — control-mode only. Lists recent Lead
  // sessions for the selected project (GET api/lead/sessions) and lets the
  // user pick one to resume (POST api/lead/resume), which closes + respawns
  // the project's Lead pane using that provider's native resume command.
  // ---------------------------------------------------------------

  function updateLeadActionVisibility() {
    var resumeBtn = $("lead-resume-btn");
    var refreshBtn = $("lead-refresh-btn");
    var inLead = state.view === "lead";
    if (resumeBtn) resumeBtn.classList.toggle("show", inLead && state.mode === "control");
    if (refreshBtn) refreshBtn.classList.toggle("show", inLead);
  }

  function resumeTimeLabel(mtime) {
    var ms = Number(mtime) * 1000;
    if (!isFinite(ms) || ms <= 0) return "";
    var d = new Date(ms);
    var hh = String(d.getHours()).padStart(2, "0");
    var mm = String(d.getMinutes()).padStart(2, "0");
    return d.toLocaleDateString() + " " + hh + ":" + mm;
  }

  function openResumeSheet() {
    var sheet = $("resume-sheet");
    var list = $("resume-sheet-list");
    if (!sheet || !list) return;
    var project = visibleProject();
    if (!project) return;
    state.resumeProject = project;
    sheet.classList.add("show");
    list.innerHTML = '<div class="resume-empty">กำลังโหลด…</div>';
    var providerLabel = $("resume-sheet-provider");
    if (providerLabel) providerLabel.textContent = "· " + providerMeta(state.providersByProject[project]).name;
    var path = "api/lead/sessions?limit=10&project=" + encodeURIComponent(project);
    apiFetch(path)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (state.resumeProject !== project) return;
        setProvider(data && data.provider, project);
        if (providerLabel) providerLabel.textContent = "· " + providerMeta(data && data.provider).name;
        renderResumeList(Array.isArray(data && data.sessions) ? data.sessions : [], project);
      })
      .catch(function () {
        list.innerHTML = '<div class="resume-empty">โหลดรายการไม่สำเร็จ ลองใหม่</div>';
      });
  }

  function closeResumeSheet() {
    var sheet = $("resume-sheet");
    if (sheet) sheet.classList.remove("show");
    state.resumeProject = "";
    state.resumePending = false;
  }

  function renderResumeList(sessions, project) {
    var list = $("resume-sheet-list");
    if (!list) return;
    list.innerHTML = "";
    if (!sessions.length) {
      list.innerHTML = '<div class="resume-empty">ยังไม่มี session ก่อนหน้าให้ resume</div>';
      return;
    }
    sessions.forEach(function (s) {
      if (!s || typeof s.uuid !== "string") return;
      var row = document.createElement("div");
      row.className = "resume-row";
      var time = document.createElement("span");
      time.className = "resume-time";
      time.textContent = resumeTimeLabel(s.mtime);
      row.appendChild(time);
      var preview = document.createElement("span");
      preview.className = "resume-preview";
      preview.textContent = (typeof s.preview === "string" && s.preview) || "(ไม่มี preview)";
      row.appendChild(preview);
      row.addEventListener("click", function () { confirmResume(s.uuid, project, row); });
      list.appendChild(row);
    });
  }

  function confirmResume(sessionUuid, project, row) {
    if (state.resumePending || !project) return;
    if (!window.confirm("Resume session นี้? Lead pane ปัจจุบันจะถูกปิดแล้วโหลด session นี้กลับมา")) return;
    state.resumePending = true;
    if (row) row.classList.add("opening");
    var body = { session_uuid: sessionUuid, project: project };
    apiFetch("api/lead/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
      .then(function (res) {
        if (res.status === 200 && res.data && res.data.ok) {
          closeResumeSheet();
          toast("กำลัง resume " + providerMeta(state.providersByProject[project]).name + " session…");
          // Reset only the resumed project's cache/transport. Other projects
          // remain connected and keep receiving messages in the background.
          stopLeadStream(project, true);
          var resumedLead = projectLeadState(project);
          var resumedMessages = Array.isArray(res.data.messages) ? res.data.messages : [];
          resumedLead.messages = [];
          resumedMessages.forEach(function (message) {
            var text = message && typeof message.text === "string" ? message.text : "";
            if (!text) return;
            resumedLead.messages.push({
              kind: message.kind === "me" ? "me" : "lead",
              text: text,
            });
          });
          resumedLead.historyLoaded = resumedLead.messages.length > 0;
          setProjectWorking(project, false, null, true);
          setProvider(res.data.provider, project);
          if (project === visibleProject()) renderSelectedProject();
          startLeadStream(project);
          return;
        }
        toast((res.data && res.data.msg) || "resume ไม่สำเร็จ");
      })
      .catch(function (err) {
        if (err instanceof Error && (err.message === "password_required" || err.message === "unauthorized")) return;
        toast("resume ไม่สำเร็จ ลองใหม่");
      })
      .then(function () {
        state.resumePending = false;
        if (row) row.classList.remove("opening");
      });
  }

  $("lead-refresh-btn").addEventListener("click", function () {
    refreshProjectHistory(visibleProject(), true);
  });
  $("lead-resume-btn").addEventListener("click", openResumeSheet);
  $("resume-sheet-close").addEventListener("click", closeResumeSheet);
  $("resume-sheet").addEventListener("click", function (evt) {
    if (evt.target === $("resume-sheet")) closeResumeSheet();
  });

  $("lead-attach").addEventListener("click", function () {
    if (state.mode === "control") $("lead-image-input").click();
  });
  $("lead-image-input").addEventListener("change", function () {
    var file = this.files && this.files[0];
    this.value = "";
    if (file) sendLeadImage(file);
  });

  $("lead-composer").addEventListener("submit", function (evt) {
    evt.preventDefault();
    // View-mode is read-only: never optimistically echo a `me` bubble that a
    // control-gated backend will reject — a stale/async mode or programmatic
    // submit could otherwise make a view-only remote look like it accepted a
    // command (codex x-check).
    if (state.mode !== "control") return;
    var input = $("lead-input");
    if (input.disabled) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    autosizeInput();
    sendLeadMessage(text);
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

  var ROLE_COLORS = {
    frontend: "#38bdf8",
    backend: "#a78bfa",
    mobile: "#fb923c",
    devops: "#f472b6",
    qa: "#4ade80",
    reviewer: "#facc15",
    critic: "#f87171",
    codex: "#93c5fd",
    gemini: "#67e8f9",
    designer: "#f0abfc",
  };

  function roleColor(role) {
    var base = String(role || "").split("#")[0].toLowerCase();
    return ROLE_COLORS[base] || "var(--accent)";
  }

  function fmtRuntime(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function fetchPulse() {
    var path = "api/activity";
    apiFetch(path)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // tolerant: unknown/missing fields are skipped, never break the view.
        var projects = (data && Array.isArray(data.projects)) ? data.projects : [];
        renderPulse(projects);
      })
      .catch(function () { /* keep last known value on transient failure */ });
  }

  function makeRoleChip(role, runtimeSec, idle, provider) {
    var chip = document.createElement("div");
    chip.className = "role-chip" + (idle ? " idle" : "");
    chip.style.setProperty("--role-color", provider ? providerMeta(provider).color : roleColor(role));

    var name = document.createElement("span");
    name.className = "role-chip-name";
    name.textContent = provider
      ? (role === "lead" ? providerIdentity(provider) : String(role) + " · " + providerIdentity(provider))
      : String(role);
    chip.appendChild(name);

    var badge = document.createElement("span");
    badge.className = "role-chip-time";
    badge.textContent = idle ? "idle" : fmtRuntime(runtimeSec);
    chip.appendChild(badge);

    return chip;
  }

  function renderPulse(projects) {
    var wrap = $("pulse-list");
    if (!wrap) return;
    wrap.innerHTML = "";

    // `roles` is always [] while the server runs LEAD_ONLY_STREAM, so the
    // headline count has to include a working Lead or it reads "0 ตำแหน่ง"
    // with a spinning Lead chip right beneath it. Counted separately from
    // `visible`: an *idle* Lead still earns its project a card (the user
    // wants to see Lead is home) but is not "working".
    var totalWorking = 0;
    var visible = 0;
    projects.forEach(function (p) {
      if (!p) return;
      if (p.lead && p.lead.provider) setProvider(p.lead.provider, p.project);
      var roleCount = Array.isArray(p.roles) ? p.roles.length : 0;
      totalWorking += roleCount;
      if (p.lead && p.lead.state === "working") totalWorking += 1;
      if (p.lead || roleCount) visible += 1;
    });

    if (!projects.length || visible === 0) {
      var empty = document.createElement("div");
      empty.className = "pulse-empty";
      empty.innerHTML = '<span class="icon">🌙</span><span class="text">ไม่มีงานกำลังรันอยู่</span>';
      wrap.appendChild(empty);
      $("pulse-count").textContent = "0 ตำแหน่งกำลังทำงาน";
      return;
    }

    $("pulse-count").textContent =
      totalWorking > 0
        ? totalWorking + " ตำแหน่งกำลังทำงาน"
        : providerMeta(state.provider).name + " ว่าง";

    projects.forEach(function (p) {
      if (!p || !p.project) return;
      var roles = Array.isArray(p.roles) ? p.roles : [];
      if (!roles.length && !p.lead) return;
      var card = document.createElement("div");
      card.className = "pulse-card";

      var header = document.createElement("div");
      header.className = "pulse-card-header";
      header.textContent = String(p.project);
      card.appendChild(header);

      var chips = document.createElement("div");
      chips.className = "pulse-chips";

      if (p.lead && p.lead.state) {
        chips.appendChild(makeRoleChip("lead", p.lead.runtime_sec, p.lead.state !== "working", p.lead.provider));
      }
      roles.forEach(function (r) {
        if (!r || !r.role) return;
        chips.appendChild(makeRoleChip(r.role, r.runtime_sec, false, r.provider));
      });
      card.appendChild(chips);

      wrap.appendChild(card);
    });
  }

  // ---------------------------------------------------------------
  // Service worker
  // ---------------------------------------------------------------

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("sw.js").catch(function () {});
    });
  }

  // Mobile browsers suspend EventSource while backgrounded. SSE does not
  // replay missed output, so refresh every warm project once on return while
  // preserving the fast no-request path for ordinary project switching.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      state.backgroundedAt = Date.now();
      return;
    }
    if (state.backgroundedAt && Date.now() - state.backgroundedAt >= 30000) {
      refreshOpenProjectHistories();
    }
    state.backgroundedAt = 0;
  });

  // ---------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------

  function enterAuthenticatedApp() {
    showApp();
    fetchProjectsAndMode().catch(function () { /* stay in view mode assumption */ });
  }

  function init() {
    if (!state.token) {
      showPairing();
      return;
    }
    // Check the optional password gate before opening history, SSE and project
    // requests. Previously all three raced ahead and produced expected 403s
    // on every reload before the password prompt appeared.
    apiFetch("api/bootstrap")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.password_required) {
          showPasswordPrompt();
          return;
        }
        enterAuthenticatedApp();
      })
      .catch(function () {
        // forgetToken() already switched to pairing on an auth 404. Keep the
        // normal retry behavior for a transient network failure.
        if (state.token) enterAuthenticatedApp();
      });
  }

  init();
})();
