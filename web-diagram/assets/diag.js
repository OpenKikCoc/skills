/* web-diagram engine: hand-placed HTML cards + runtime-measured SVG connectors.
   - Box diagram: <div class="diag" data-w data-h> is a logical stage scaled to its
     container width. Edges are a JSON list in <script type="application/json" class="edges">.
   - Sequence diagram: <div class="diag seq" data-w> with a JSON definition in
     <script type="application/json" class="seqdef">.
   Public API: window.diagFitAll() re-fits every stage to its container (call it after
   showing a hidden diagram or changing container width). No dependencies. */
(function () {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var COLORS = { primary: '#5b5bd6', data: '#0f9d8a', outcome: '#249c62', feedback: '#d97706',
                 bulk: '#94a3b8', danger: '#dc2626', tree: '#94a3b8' };

  function ensureDefs() {
    if (document.getElementById('diag-defs')) return;
    var svg = document.createElementNS(NS, 'svg');
    svg.id = 'diag-defs'; svg.setAttribute('width', 0); svg.setAttribute('height', 0);
    svg.style.position = 'absolute';
    var defs = document.createElementNS(NS, 'defs');
    Object.keys(COLORS).forEach(function (k) {
      var m = document.createElementNS(NS, 'marker');
      m.id = 'arr-' + k; m.setAttribute('markerWidth', 10); m.setAttribute('markerHeight', 8);
      m.setAttribute('refX', 8); m.setAttribute('refY', 4); m.setAttribute('orient', 'auto');
      m.setAttribute('markerUnits', 'userSpaceOnUse');
      var p = document.createElementNS(NS, 'path');
      p.setAttribute('d', 'M0,0 L10,4 L0,8 Z'); p.setAttribute('fill', COLORS[k]);
      m.appendChild(p); defs.appendChild(m);
    });
    svg.appendChild(defs); document.body.insertBefore(svg, document.body.firstChild);
  }

  /* ---------- geometry (mirrored in scripts/verify_diagram.py: keep in sync) ---------- */
  function box(root, id) {
    var el = root.querySelector('[data-node="' + id + '"]');
    if (!el) throw new Error('no node ' + id);
    return { left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight,
             right: el.offsetLeft + el.offsetWidth, bottom: el.offsetTop + el.offsetHeight };
  }
  function anchor(b, side, at) {
    at = (at == null) ? 0.5 : at;
    if (side === 'left')  return { x: b.left,  y: b.top + b.height * at, h: true,  dir: -1 };
    if (side === 'right') return { x: b.right, y: b.top + b.height * at, h: true,  dir: 1 };
    if (side === 'top')   return { x: b.left + b.width * at, y: b.top,    h: false, dir: -1 };
    return                       { x: b.left + b.width * at, y: b.bottom, h: false, dir: 1 };
  }
  function defaultSides(a, b) {
    var dx = (b.left + b.width / 2) - (a.left + a.width / 2);
    var dy = (b.top + b.height / 2) - (a.top + a.height / 2);
    if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? ['right', 'left'] : ['left', 'right'];
    return dy > 0 ? ['bottom', 'top'] : ['top', 'bottom'];
  }
  /* Orthogonal route. `out` is how far (px) a same-side loop leaves the boxes before turning. */
  function route(p1, p2, out) {
    out = out || 40;
    var pts = [[p1.x, p1.y]];
    if (p1.h && p2.h) {
      var mx;
      if (p1.dir === p2.dir) mx = p1.dir > 0 ? Math.max(p1.x, p2.x) + out : Math.min(p1.x, p2.x) - out;
      else mx = (p1.x + p2.x) / 2;
      pts.push([mx, p1.y]); pts.push([mx, p2.y]);
    } else if (!p1.h && !p2.h) {
      var my;
      if (p1.dir === p2.dir) my = p1.dir > 0 ? Math.max(p1.y, p2.y) + out : Math.min(p1.y, p2.y) - out;
      else my = (p1.y + p2.y) / 2;
      pts.push([p1.x, my]); pts.push([p2.x, my]);
    } else if (p1.h && !p2.h) {
      pts.push([p2.x, p1.y]);
    } else {
      pts.push([p1.x, p2.y]);
    }
    pts.push([p2.x, p2.y]);
    return pts;
  }
  /* Point halfway along the polyline (where the edge label is centred). */
  function midpoint(pts) {
    var total = 0, segs = [];
    for (var i = 1; i < pts.length; i++) {
      var l = Math.abs(pts[i][0] - pts[i - 1][0]) + Math.abs(pts[i][1] - pts[i - 1][1]);
      segs.push(l); total += l;
    }
    var half = total / 2;
    for (var j = 0; j < segs.length; j++) {
      if (half <= segs[j]) {
        var t = segs[j] ? half / segs[j] : 0;
        return [pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t, pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t];
      }
      half -= segs[j];
    }
    return pts[pts.length - 1];
  }
  function pathD(pts) { return pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0] + ' ' + p[1]; }).join(' '); }

  /* ---------- box diagrams ---------- */
  function drawBoxDiagram(diag) {
    var W = +diag.dataset.w, H = +diag.dataset.h;
    var svg = diag.querySelector('svg.edges');
    if (!svg) { svg = document.createElementNS(NS, 'svg'); svg.setAttribute('class', 'edges'); diag.appendChild(svg); }
    svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    diag.querySelectorAll('.edge-label').forEach(function (l) { l.remove(); });
    var def = diag.querySelector('script.edges');
    if (!def) return;
    var edges = JSON.parse(def.textContent);
    edges.forEach(function (e) {
      try {
        var kind = e.kind || 'primary';
        var pts;
        if (e.points) { pts = e.points; }
        else {
          var a = box(diag, e.from), b = box(diag, e.to);
          var sides = defaultSides(a, b);
          var p1 = anchor(a, e.fromSide || sides[0], e.fromAt), p2 = anchor(b, e.toSide || sides[1], e.toAt);
          pts = route(p1, p2, e.out);
        }
        var path = document.createElementNS(NS, 'path');
        path.setAttribute('d', pathD(pts));
        path.setAttribute('class', 'edge ' + kind + (e.thick ? ' thick' : ''));
        if (!e.noarrow) path.setAttribute('marker-end', 'url(#arr-' + kind + ')');
        svg.appendChild(path);
        if (e.label) {
          var m = midpoint(pts);
          var lab = document.createElement('div');
          lab.className = 'edge-label ' + kind;
          lab.innerHTML = e.label;
          lab.style.left = (m[0] + (e.dx || 0)) + 'px';
          lab.style.top = (m[1] + (e.dy || 0)) + 'px';
          diag.appendChild(lab);
        }
      } catch (err) { console.error('diag edge skipped', e, err); }
    });
  }

  /* ---------- sequence diagrams ---------- */
  function drawSeq(diag) {
    var defEl = diag.querySelector('script.seqdef');
    var def = JSON.parse(defEl.textContent);
    var W = +diag.dataset.w || 1100;
    var P = def.participants, n = P.length;
    var padX = 24, headH = 46, rowH = def.rowH || 40, colW = (W - padX * 2) / n;
    var xs = P.map(function (_, i) { return padX + colW * i + colW / 2; });
    var idx = {}; P.forEach(function (p, i) { idx[p.id] = i; });
    Array.prototype.slice.call(diag.children).forEach(function (c) { if (c !== defEl) diag.removeChild(c); });
    var y = headH + 30;
    var frames = [];
    function walk(items, depth) {
      items.forEach(function (m) {
        if (m.loop || m.alt) {
          var startY = y; y += 26;
          walk(m.items, depth + 1);
          y += 10;
          frames.push({ y0: startY, y1: y, label: m.loop ? 'loop' : 'alt', text: m.loop || m.alt, depth: depth });
          y += 8;
          return;
        }
        m._y = y;
        if (m.note) { y += (m.tall ? 72 : 58); } else { y += (m.from === m.to ? rowH + 14 : rowH); }
      });
    }
    walk(def.messages, 0);
    var H = y + 10;
    diag.dataset.h = H; diag.style.height = H + 'px';
    var svg = document.createElementNS(NS, 'svg'); svg.setAttribute('class', 'edges');
    svg.setAttribute('width', W); svg.setAttribute('height', H); svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    diag.appendChild(svg);
    P.forEach(function (p, i) {
      var hw = Math.min(colW - 10, 170);
      var head = document.createElement('div');
      head.className = 'seq-head' + (p.kind ? ' k-' + p.kind : '');
      head.style.left = (xs[i] - hw / 2) + 'px'; head.style.width = hw + 'px';
      head.style.top = '8px'; head.style.height = headH + 'px';
      head.innerHTML = p.label; diag.appendChild(head);
      var line = document.createElementNS(NS, 'line');
      line.setAttribute('x1', xs[i]); line.setAttribute('x2', xs[i]);
      line.setAttribute('y1', headH + 10); line.setAttribute('y2', H - 4);
      line.setAttribute('class', 'lifeline'); svg.appendChild(line);
    });
    frames.forEach(function (f) {
      var fr = document.createElement('div'); fr.className = 'seq-frame';
      var inset = 14 + f.depth * 12;
      fr.style.left = inset + 'px'; fr.style.width = (W - inset * 2) + 'px';
      fr.style.top = f.y0 + 'px'; fr.style.height = (f.y1 - f.y0) + 'px';
      fr.innerHTML = '<span class="seq-frame-tag">' + f.label + '</span><span class="seq-frame-text">' + f.text + '</span>';
      diag.appendChild(fr);
    });
    function walkDraw(items) {
      items.forEach(function (m) {
        if (m.loop || m.alt) { walkDraw(m.items); return; }
        var yy = m._y;
        if (m.note) {
          var over = m.over.map(function (id) { return xs[idx[id]]; });
          var x0 = Math.min.apply(null, over) - 120, x1 = Math.max.apply(null, over) + 120;
          if (x0 < 8) { x1 += 8 - x0; x0 = 8; }
          if (x1 > W - 8) { x0 -= x1 - (W - 8); x1 = W - 8; }
          var nb = document.createElement('div'); nb.className = 'seq-note';
          nb.style.left = x0 + 'px'; nb.style.width = (x1 - x0) + 'px'; nb.style.top = (yy + 2) + 'px';
          nb.innerHTML = m.note; diag.appendChild(nb); return;
        }
        var a = idx[m.from], b = idx[m.to];
        var path = document.createElementNS(NS, 'path');
        var color = m.reply ? 'data' : (m.danger ? 'danger' : 'primary');
        if (a === b) {
          var x = xs[a];
          path.setAttribute('d', 'M' + x + ' ' + (yy + 6) + ' H' + (x + 30) + ' V' + (yy + 24) + ' H' + (x + 4));
        } else {
          var xa = xs[a], xb = xs[b], dir = xb > xa ? 1 : -1;
          path.setAttribute('d', 'M' + xa + ' ' + (yy + 18) + ' H' + (xb - dir * 6));
        }
        path.setAttribute('class', 'msg ' + color + (m.reply ? ' dashed' : ''));
        path.setAttribute('marker-end', 'url(#arr-' + color + ')');
        svg.appendChild(path);
        var lab = document.createElement('div'); lab.className = 'seq-label' + (m.reply ? ' reply' : '');
        lab.innerHTML = m.label;
        if (a === b) { lab.style.left = (xs[a] + 40) + 'px'; lab.style.top = (yy + 4) + 'px'; lab.style.transform = 'none'; }
        else { lab.style.left = ((xs[a] + xs[b]) / 2) + 'px'; lab.style.top = (yy + 2) + 'px'; }
        diag.appendChild(lab);
      });
    }
    walkDraw(def.messages);
  }

  /* ---------- fit + init ---------- */
  function fitAll() {
    document.querySelectorAll('.diag-wrap').forEach(function (wrap) {
      if (wrap.closest('dialog')) return;
      var diag = wrap.querySelector('.diag');
      if (!diag) return;
      var W = +diag.dataset.w || 1100, H = +diag.dataset.h;
      var s = Math.min(1, wrap.clientWidth / W);
      diag.style.transform = 'scale(' + s + ')';
      wrap.style.height = (H * s) + 'px';
      diag.style.marginLeft = ((wrap.clientWidth - W * s) / 2) + 'px';
    });
  }
  function safe(fn) { return function (d) { try { fn(d); } catch (e) { console.error('diagram skipped', d, e); } }; }
  function init() {
    ensureDefs();
    document.querySelectorAll('.diag.seq').forEach(safe(drawSeq));
    document.querySelectorAll('.diag:not(.seq)').forEach(safe(drawBoxDiagram));
    fitAll();
    /* second pass once fonts/layout settled: offsetWidth/Height may change after first paint */
    requestAnimationFrame(function () {
      document.querySelectorAll('.diag:not(.seq)').forEach(safe(drawBoxDiagram));
      fitAll();
    });
  }
  window.diagFitAll = fitAll;
  window.addEventListener('resize', fitAll);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
