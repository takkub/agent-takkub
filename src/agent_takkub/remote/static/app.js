"use strict";

/*
 * Takkub Remote — vanilla JS SPA, no build step, no external requests.
 * Data-minimization contract (design doc §7.3): only ever render (1) the
 * Lead's own conversation/report text and (2) on Pulse, role name + runtime
 * per project. Any other field the API might accidentally include (task
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
    es: null,
    esRetries: 0,
    esTimer: null,
    historyLoaded: false,
    pulseTimer: null,
    lastToast: 0,
    activeProject: "",
    selectedProject: "",
    openTabs: [],
    opening: null,
    closing: null,
  };

  var VIEW_LABELS = { projects: "Projects", lead: "Lead", pulse: "Pulse" };

  var VIEW_SUBTITLES = {
    projects: "เฉพาะที่ import ไว้",
    lead: "", // dynamic based on activeProject
    pulse: "เห็นแค่จำนวน"
  };

  function updateHeaderTitle() {
    var label = VIEW_LABELS[state.view] || "Takkub Remote";
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
    if (name === "lead") startLeadStream();
    if (name === "pulse") startPulsePolling();
    else stopPulsePolling();
    if (name !== "lead") stopLeadStream();
    updateResumeButtonVisibility();
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
            showApp();
            fetchProjectsAndMode().catch(function () { /* stay in view mode assumption */ });
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

  // Switch the mobile view to a different open project: rebinds the SSE
  // ticket/stream, refreshes pulse, and points the composer at it — then
  // jumps straight to the Lead view.
  function selectProject(name) {
    if (!name) return;
    if (name === state.selectedProject) {
      switchView("lead");
      return;
    }
    state.selectedProject = name;
    lastMsgKind = null;
    lastLeadBodyEl = null;
    lastLeadRawAccum = "";
    hidePickerBanner();
    hideThinking();
    var log = $("lead-log");
    if (log) {
      var old = log.querySelectorAll(".msg");
      for (var i = 0; i < old.length; i++) old[i].remove();
    }
    updateHeaderTitle();
    stopLeadStream();
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

  var MAX_ES_RETRIES = 5;
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

  // Inline markdown (bold/italic/strike/code/links) — always escapes first,
  // then applies patterns to the *escaped* string so a `<script>` or `**` in
  // user/Lead text can never become live markup.
  function mdInline(raw) {
    var s = mdEscape(raw);
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
  var THINKING_DEFAULT_LABEL = "⏳ Lead กำลังทำงาน…";
  function thinkingLabelFor(category) {
    return (category && THINKING_LABELS[category]) || THINKING_DEFAULT_LABEL;
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
    who.appendChild(document.createTextNode("Lead"));
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

  function appendMsg(kind, text) {
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
      who.appendChild(document.createTextNode("Lead"));
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

  function appendLeadLive(text) {
    if (typeof text !== "string" || !text) return;
    hidePickerBanner();
    var now = Date.now();
    if (lastLeadBodyEl && lastMsgKind === "lead" && (now - lastLeadAt) <= LEAD_MERGE_WINDOW_MS) {
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
    appendMsg("lead", text);
    lastLeadAt = now;
    var bodies = document.querySelectorAll("#lead-log .msg.lead .msg-body");
    lastLeadBodyEl = bodies.length ? bodies[bodies.length - 1] : null;
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

  // B2: renders tappable option chips from the AskUserQuestion payload
  // instead of forcing the phone user to answer on the desktop. Only
  // understands Claude's JSONL AskUserQuestion shape (server-side
  // notify.py) — codex/gemini panes have no structured picker event, so
  // those still fall back to the plain "answer on desktop" text below
  // (multi-provider #103 gap).
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
    appendMsg("me", text);
    showThinking();
    var sayBody = { text: text };
    if (state.selectedProject) sayBody.project = state.selectedProject;
    apiFetch("api/lead/say", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sayBody),
    }).catch(function () {
      hideThinking();
      toast("ส่งข้อความไม่สำเร็จ");
    });
  }

  function setLeadEmptyText(text) {
    var emptyEl = $("lead-empty");
    if (emptyEl) {
      var txtEl = emptyEl.querySelector(".text");
      if (txtEl) txtEl.textContent = text;
      else emptyEl.textContent = text;
    }
  }

  // Repopulates the chat log from GET /api/lead/history before opening the
  // SSE stream — the live tail only ever reaches currently-connected
  // clients, so without this a fresh connect/reconnect/project-switch shows
  // a blank screen and loses whatever reply landed during the gap.
  function loadHistory() {
    var path = "api/lead/history?limit=200";
    if (state.selectedProject) path += "&project=" + encodeURIComponent(state.selectedProject);
    return apiFetch(path)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // Full re-fetch — clear rendered messages first or a tab-switch-back/
        // reconnect re-appends the same history on top of what's already
        // there. Only strips .msg nodes so #lead-empty (a sibling, shown via
        // setLeadEmptyText) survives to be reused if history comes back empty.
        hideThinking();
        var log = $("lead-log");
        if (log) {
          var old = log.querySelectorAll(".msg");
          for (var i = 0; i < old.length; i++) old[i].remove();
        }
        lastMsgKind = null;
        lastLeadBodyEl = null;
        lastLeadRawAccum = "";
        hidePickerBanner();
        var messages = Array.isArray(data && data.messages) ? data.messages : [];
        messages.forEach(function (m) {
          var text = m && typeof m.text === "string" ? m.text : null;
          if (text) appendMsg(m && m.kind === "me" ? "me" : "lead", text);
        });
      })
      .catch(function () { /* best-effort — live SSE still works without history */ });
  }

  function startLeadStream() {
    updateControlNote();
    // History load is decoupled from the connect guard below on purpose: a
    // stale esTimer (e.g. left over from a pre-auth 403 retry loop) must
    // never block the chat log from repopulating once we're actually able
    // to connect — that was Bug B (history stuck empty after the password
    // gate). historyLoaded is reset in stopLeadStream so each fresh entry
    // into the lead view (or project switch) reloads once.
    if (!state.historyLoaded) {
      state.historyLoaded = true;
      setLeadEmptyText("กำลังเชื่อมต่อ…");
      loadHistory();
    }
    if (state.es || state.esTimer) return;
    requestTicketAndConnect();
  }

  function requestTicketAndConnect() {
    var body = state.selectedProject ? { project: state.selectedProject } : {};
    apiFetch("api/sse-ticket", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
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
      setOffline(false);
      // Reset grouping across a (re)connect: the next message must start a
      // fresh run with its own Lead label + timestamp, never fold silently
      // into the pre-reconnect run (codex x-check).
      lastMsgKind = null;
      lastLeadBodyEl = null;
      lastLeadRawAccum = "";
      hidePickerBanner();
      state.leadWorking = false;
      setLeadEmptyText("ยังไม่มีข้อความ — พิมพ์ถึง Lead ด้านล่างเพื่อเริ่ม");
    };
    // Backend sends 'working' whenever the Lead is actively doing something
    // (tool_use/thinking) with no reply text yet — payload is a JSON
    // {text: category} where category is one of reading/editing/running/
    // web/delegating/skill/working, mapped to a Thai label so the remote
    // shows *what* the Lead is doing, not just a bare "…".
    es.addEventListener("working", function (evt) {
      state.leadWorking = true;
      showThinking(parseSseData(evt.data));
    });
    // Lead pane went idle (turn finished) — drop the "…" the instant the
    // desktop spinner stops, so the phone never shows a stale "working" state.
    es.addEventListener("idle", function () {
      state.leadWorking = false;
      hideThinking();
    });
    es.addEventListener("lead", function (evt) {
      appendLeadLive(parseSseData(evt.data));
    });
    es.addEventListener("done", function (evt) {
      state.leadWorking = false;
      appendMsg("done", parseSseData(evt.data));
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
      showPickerBanner(payload);
    });
    es.onerror = function () {
      es.close();
      state.es = null;
      setLeadEmptyText("การเชื่อมต่อขัดข้อง — กำลังพยายามเชื่อมต่อใหม่…");
      scheduleEsRetry();
    };
  }

  function scheduleEsRetry() {
    if (state.view !== "lead") return;
    state.esRetries += 1;
    if (state.esRetries > MAX_ES_RETRIES) {
      setLeadEmptyText("เชื่อมต่อไม่ได้ กรุณาสแกน QR ใหม่");
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
    state.historyLoaded = false;
    state.leadWorking = false;
    hideThinking();
    hidePickerBanner();
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
    renderQuickReplies();
    // Picker chips (B2) were rendered with isControl baked in at showPickerBanner
    // time — if the mode toggles while a banner is still showing (no new picker
    // event to re-render it), flip their disabled state directly instead of
    // leaving stale-enabled chips a view-mode user could still tap.
    document.querySelectorAll("#lead-picker-banner button").forEach(function (btn) {
      btn.disabled = !isControl;
    });
    updateResumeButtonVisibility();
  }

  // ---------------------------------------------------------------
  // Resume / session picker (W3) — control-mode only. Lists recent Lead
  // sessions for the selected project (GET api/lead/sessions) and lets the
  // user pick one to resume (POST api/lead/resume), which closes + respawns
  // the project's Lead pane on the desktop with `--resume <uuid>`.
  // ---------------------------------------------------------------

  function updateResumeButtonVisibility() {
    var btn = $("lead-resume-btn");
    if (!btn) return;
    btn.classList.toggle("show", state.view === "lead" && state.mode === "control");
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
    sheet.classList.add("show");
    list.innerHTML = '<div class="resume-empty">กำลังโหลด…</div>';
    var path = "api/lead/sessions?limit=10";
    if (state.selectedProject) path += "&project=" + encodeURIComponent(state.selectedProject);
    apiFetch(path)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderResumeList(Array.isArray(data && data.sessions) ? data.sessions : []);
      })
      .catch(function () {
        list.innerHTML = '<div class="resume-empty">โหลดรายการไม่สำเร็จ ลองใหม่</div>';
      });
  }

  function closeResumeSheet() {
    var sheet = $("resume-sheet");
    if (sheet) sheet.classList.remove("show");
  }

  function renderResumeList(sessions) {
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
      row.addEventListener("click", function () { confirmResume(s.uuid); });
      list.appendChild(row);
    });
  }

  function confirmResume(sessionUuid) {
    if (!window.confirm("Resume session นี้? Lead pane ปัจจุบันจะถูกปิดแล้วโหลด session นี้กลับมา")) return;
    var body = { session_uuid: sessionUuid };
    if (state.selectedProject) body.project = state.selectedProject;
    apiFetch("api/lead/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
      .then(function (res) {
        if (res.status === 200 && res.data && res.data.ok) {
          closeResumeSheet();
          toast("กำลัง resume session…");
          stopLeadStream();
          startLeadStream();
          return;
        }
        toast((res.data && res.data.msg) || "resume ไม่สำเร็จ");
      })
      .catch(function (err) {
        if (err instanceof Error && (err.message === "password_required" || err.message === "unauthorized")) return;
        toast("resume ไม่สำเร็จ ลองใหม่");
      });
  }

  $("lead-resume-btn").addEventListener("click", openResumeSheet);
  $("resume-sheet-close").addEventListener("click", closeResumeSheet);
  $("resume-sheet").addEventListener("click", function (evt) {
    if (evt.target === $("resume-sheet")) closeResumeSheet();
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

  function makeRoleChip(role, runtimeSec, idle) {
    var chip = document.createElement("div");
    chip.className = "role-chip" + (idle ? " idle" : "");
    chip.style.setProperty("--role-color", roleColor(role));

    var name = document.createElement("span");
    name.className = "role-chip-name";
    name.textContent = String(role);
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

    var totalRoles = 0;
    var totalWorking = 0;
    projects.forEach(function (p) {
      if (!p) return;
      if (Array.isArray(p.roles)) totalRoles += p.roles.length;
      if (p.lead) totalWorking += 1; // every open Lead counts as a visible position
      totalWorking += (Array.isArray(p.roles) ? p.roles.length : 0);
    });

    if (!projects.length || totalWorking === 0) {
      var empty = document.createElement("div");
      empty.className = "pulse-empty";
      empty.innerHTML = '<span class="icon">🌙</span><span class="text">ไม่มีงานกำลังรันอยู่</span>';
      wrap.appendChild(empty);
      $("pulse-count").textContent = "0 ตำแหน่งกำลังทำงาน";
      return;
    }

    $("pulse-count").textContent = totalRoles + " ตำแหน่งกำลังทำงาน";

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
        chips.appendChild(makeRoleChip("lead", p.lead.runtime_sec, p.lead.state !== "working"));
      }
      roles.forEach(function (r) {
        if (!r || !r.role) return;
        chips.appendChild(makeRoleChip(r.role, r.runtime_sec, false));
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
