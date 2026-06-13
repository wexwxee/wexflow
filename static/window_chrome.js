/* WexFlow frameless window controls.
   pywebview does not give Windows a native titlebar when frameless=true, so
   moving and resizing are wired explicitly through the Python js_api bridge. */
(function () {
  function api() {
    var p = window.pywebview;
    return p && p.api ? p.api : null;
  }

  function ready(fn) {
    if (api()) {
      fn();
      return;
    }
    var done = false;
    var started = Date.now();
    function run() {
      if (done || !api()) return;
      done = true;
      fn();
    }
    window.addEventListener("pywebviewready", run, { once: true });
    var timer = window.setInterval(function () {
      if (done) {
        window.clearInterval(timer);
        return;
      }
      run();
      if (Date.now() - started > 10000) window.clearInterval(timer);
    }, 50);
  }

  ready(function () {
    var bridge = api();
    if (!bridge) return;

    var bar = document.querySelector(".desktop-framebar");
    var interactive = ".desktop-traffic, .desktop-traffic *, button, a, input, select, textarea";

    // Move only from the custom top bar, never from the whole page.
    if (bar && bridge.move_window_by) {
      var dragging = false;
      var lastX = 0;
      var lastY = 0;
      var moveDx = 0;
      var moveDy = 0;
      var movePending = false;

      function flushMove() {
        movePending = false;
        if (!dragging || (!moveDx && !moveDy)) return;
        var dx = moveDx;
        var dy = moveDy;
        moveDx = 0;
        moveDy = 0;
        var a = api();
        if (a && a.move_window_by) a.move_window_by(dx, dy);
      }

      function onDragMove(e) {
        if (!dragging) return;
        e.preventDefault();
        moveDx += e.screenX - lastX;
        moveDy += e.screenY - lastY;
        lastX = e.screenX;
        lastY = e.screenY;
        if (!movePending) {
          movePending = true;
          requestAnimationFrame(flushMove);
        }
      }

      function onDragEnd() {
        dragging = false;
        document.removeEventListener("mousemove", onDragMove, true);
        document.removeEventListener("mouseup", onDragEnd, true);
        document.body.classList.remove("wex-window-dragging");
      }

      bar.addEventListener("mousedown", function (e) {
        if (e.button !== 0 || e.target.closest(interactive)) return;
        dragging = true;
        lastX = e.screenX;
        lastY = e.screenY;
        moveDx = 0;
        moveDy = 0;
        e.preventDefault();
        document.body.classList.add("wex-window-dragging");
        document.addEventListener("mousemove", onDragMove, true);
        document.addEventListener("mouseup", onDragEnd, true);
      });

      bar.addEventListener("dblclick", function (e) {
        if (e.target.closest(interactive)) return;
        var a = api();
        if (a && a.toggle_maximize) a.toggle_maximize();
      });
    }

    // Resize from a thin invisible border around the window.
    if (!bridge.resize_window) return;

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

    var MINW = 900;
    var MINH = 600;
    var active = null;
    var anchor = "nw";
    var startX = 0;
    var startY = 0;
    var startW = 0;
    var startH = 0;
    var lastW = 0;
    var lastH = 0;
    var resizePending = false;

    function onResizeMove(e) {
      if (!active) return;
      e.preventDefault();
      var cfg = H[active];
      var dx = e.screenX - startX;
      var dy = e.screenY - startY;
      lastW = Math.max(MINW, Math.round(startW + cfg.dw * dx));
      lastH = Math.max(MINH, Math.round(startH + cfg.dh * dy));
      if (resizePending) return;
      resizePending = true;
      requestAnimationFrame(function () {
        resizePending = false;
        var a = api();
        if (a && a.resize_window) a.resize_window(lastW, lastH, anchor);
      });
    }

    function onResizeEnd() {
      active = null;
      document.removeEventListener("mousemove", onResizeMove, true);
      document.removeEventListener("mouseup", onResizeEnd, true);
      document.body.classList.remove("wex-window-resizing");
    }

    Object.keys(H).forEach(function (dir) {
      var cfg = H[dir];
      var el = document.createElement("div");
      el.className = "wex-resize wex-resize-" + dir;
      el.style.position = "fixed";
      el.style.zIndex = "2147483000";
      el.style.background = "transparent";
      el.style.userSelect = "none";
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
        document.addEventListener("mousemove", onResizeMove, true);
        document.addEventListener("mouseup", onResizeEnd, true);
      });
      document.body.appendChild(el);
    });
  });
})();
