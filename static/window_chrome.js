/* WexFlow frameless window resize.
   Move is handled natively by pywebview's drag-region (class on .desktop-drag).
   Resize has no native equivalent for a frameless WebView2 window, so we add a
   thin invisible border that calls the Python js_api (window.resize via FixPoint). */
(function () {
  function api() {
    var p = window.pywebview;
    return p && p.api ? p.api : null;
  }

  function ready(fn) {
    if (api()) { fn(); return; }
    window.addEventListener("pywebviewready", fn, { once: true });
    var n = 0;
    var t = window.setInterval(function () {
      if (api()) { window.clearInterval(t); fn(); }
      else if (++n > 200) window.clearInterval(t);
    }, 50);
  }

  ready(function () {
    var bridge = api();
    if (!bridge || !bridge.resize_window) return;

    // double-click the top bar toggles maximize
    var bar = document.querySelector(".desktop-framebar");
    if (bar) {
      bar.addEventListener("dblclick", function (e) {
        if (e.target.closest("button, a, input, select, textarea")) return;
        var a = api();
        if (a && a.toggle_maximize) a.toggle_maximize();
      });
    }

    var H = {
      n:  { a: "sw", dw: 0,  dh: -1, s: { top: 0, left: 0, right: 0, height: "7px", cursor: "ns-resize" } },
      s:  { a: "nw", dw: 0,  dh: 1,  s: { bottom: 0, left: 0, right: 0, height: "7px", cursor: "ns-resize" } },
      e:  { a: "nw", dw: 1,  dh: 0,  s: { top: 0, bottom: 0, right: 0, width: "7px", cursor: "ew-resize" } },
      w:  { a: "ne", dw: -1, dh: 0,  s: { top: 0, bottom: 0, left: 0, width: "7px", cursor: "ew-resize" } },
      ne: { a: "sw", dw: 1,  dh: -1, s: { top: 0, right: 0, width: "16px", height: "16px", cursor: "nesw-resize" } },
      nw: { a: "se", dw: -1, dh: -1, s: { top: 0, left: 0, width: "16px", height: "16px", cursor: "nwse-resize" } },
      se: { a: "nw", dw: 1,  dh: 1,  s: { bottom: 0, right: 0, width: "16px", height: "16px", cursor: "nwse-resize" } },
      sw: { a: "ne", dw: -1, dh: 1,  s: { bottom: 0, left: 0, width: "16px", height: "16px", cursor: "nesw-resize" } }
    };

    var MINW = 900, MINH = 600;
    var active = null, anchor = "nw";
    var startX = 0, startY = 0, startW = 0, startH = 0;
    var lastW = 0, lastH = 0, pending = false;

    function onMove(e) {
      if (!active) return;
      e.preventDefault();
      var cfg = H[active];
      var dx = e.screenX - startX;
      var dy = e.screenY - startY;
      lastW = Math.max(MINW, Math.round(startW + cfg.dw * dx));
      lastH = Math.max(MINH, Math.round(startH + cfg.dh * dy));
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        var a = api();
        if (a && a.resize_window) a.resize_window(lastW, lastH, anchor);
      });
    }

    function onUp() {
      active = null;
      document.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("mouseup", onUp, true);
      document.body.classList.remove("wex-window-resizing");
    }

    Object.keys(H).forEach(function (dir) {
      var cfg = H[dir];
      var el = document.createElement("div");
      el.className = "wex-resize wex-resize-" + dir;
      el.style.position = "fixed";
      el.style.zIndex = "2147483000";
      el.style.background = "transparent";
      Object.keys(cfg.s).forEach(function (k) {
        var v = cfg.s[k];
        el.style[k] = typeof v === "number" ? v + "px" : v;
      });
      el.addEventListener("mousedown", function (e) {
        if (e.button !== 0) return;
        e.preventDefault();
        active = dir;
        anchor = cfg.a;
        startX = e.screenX;
        startY = e.screenY;
        startW = window.innerWidth;
        startH = window.innerHeight;
        document.body.classList.add("wex-window-resizing");
        document.addEventListener("mousemove", onMove, true);
        document.addEventListener("mouseup", onUp, true);
      });
      document.body.appendChild(el);
    });
  });
})();
