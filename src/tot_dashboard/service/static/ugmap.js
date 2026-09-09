/* UrbanGuard 배경지도 — XYZ 타일을 직접 그린다.
 *
 * 왜 라이브러리를 안 쓰나
 *   이 제품은 **망분리 환경 설치를 전제**로 한다. CDN에서 스크립트를 받아
 *   오는 구조는 관제망에서 그대로 실패한다. 파일을 함께 넣는 방법도 있지만,
 *   슬리피 맵에 필요한 계산은 아래 60줄이 전부라 의존성을 늘릴 이유가 없다.
 *
 * 무엇을 하나
 *   웹 메르카토르 타일을 <img> 로 깔고 그 위에 지점을 찍는다. 끌어서 이동,
 *   버튼과 휠로 확대·축소.
 *
 * 실패하면
 *   타일을 하나도 못 받으면 **기존 배치 도식으로 되돌린다.** 빈 회색 상자를
 *   보여 주면 관제요원은 지도가 고장인지 지점이 없는지 알 수 없다.
 */
(function (global) {
  'use strict';

  var TILE = 256;

  function lngToWorldX(lng, z) {
    return (lng + 180) / 360 * Math.pow(2, z);
  }

  function latToWorldY(lat, z) {
    // 메르카토르는 극지방에서 발산한다. 국내 범위에선 무관하지만 잘라 둔다.
    var r = Math.max(-85.05112878, Math.min(85.05112878, lat)) * Math.PI / 180;
    return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * Math.pow(2, z);
  }

  function UGMap(el, opts) {
    this.el = el;
    this.tileUrl = opts.tileUrl || '';
    this.maxZoom = opts.maxZoom || 18;
    this.minZoom = 5;
    this.points = opts.points || [];
    this.onFail = opts.onFail || function () {};
    this.attribution = opts.attribution || '';
    this.tilesTried = 0;
    this.tilesFailed = 0;
    this.tilesLoaded = 0;
    this.failReported = false;
    this._build();
    this._fit();
    this.render();

    // **응답이 아예 안 오는 경우**를 잡는다. 차단된 망에서는 error 이벤트조차
    // 뜨지 않고 요청이 그대로 매달린다 — 그때 빈 회색 상자만 남으면
    // 관제요원은 지도가 고장인지 지점이 없는지 알 수 없다.
    var self = this;
    this._deadline = setTimeout(function () {
      if (!self.failReported && self.tilesLoaded === 0) {
        self.failReported = true;
        self.onFail();
      }
    }, opts.timeoutMs || 6000);
  }

  /** 되돌린 뒤에는 타이머를 남기지 않는다. */
  UGMap.prototype.destroy = function () {
    clearTimeout(this._deadline);
  };

  UGMap.prototype._build = function () {
    var self = this;
    this.el.classList.add('ug-tilemap');
    this.el.innerHTML = '';

    this.pane = document.createElement('div');
    this.pane.className = 'ug-tilemap-pane';
    this.el.appendChild(this.pane);

    this.markerPane = document.createElement('div');
    this.markerPane.className = 'ug-tilemap-markers';
    this.el.appendChild(this.markerPane);

    var ctrl = document.createElement('div');
    ctrl.className = 'ug-tilemap-ctrl';
    [['+', 1, '확대'], ['−', -1, '축소']].forEach(function (spec) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = spec[0];
      b.setAttribute('aria-label', spec[2]);
      b.addEventListener('click', function () { self.zoomBy(spec[1]); });
      ctrl.appendChild(b);
    });
    this.el.appendChild(ctrl);

    if (this.attribution) {
      var a = document.createElement('div');
      a.className = 'ug-tilemap-attr';
      a.textContent = this.attribution;   // 저작권 표기는 대부분 의무다
      this.el.appendChild(a);
    }

    // 끌어서 이동
    var dragging = false, lastX = 0, lastY = 0;
    this.el.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.ug-tilemap-ctrl')) { return; }
      dragging = true; lastX = e.clientX; lastY = e.clientY;
      self.el.setPointerCapture(e.pointerId);
      self.el.classList.add('is-dragging');
    });
    this.el.addEventListener('pointermove', function (e) {
      if (!dragging) { return; }
      self.centerX -= (e.clientX - lastX) / TILE;
      self.centerY -= (e.clientY - lastY) / TILE;
      lastX = e.clientX; lastY = e.clientY;
      self.render();
    });
    ['pointerup', 'pointercancel'].forEach(function (ev) {
      self.el.addEventListener(ev, function () {
        dragging = false;
        self.el.classList.remove('is-dragging');
      });
    });
    this.el.addEventListener('wheel', function (e) {
      e.preventDefault();
      self.zoomBy(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });
  };

  /** 모든 지점이 들어오도록 중심과 배율을 정한다. */
  UGMap.prototype._fit = function () {
    var pts = this.points.filter(function (p) {
      return typeof p.lat === 'number' && typeof p.lng === 'number';
    });
    if (!pts.length) {
      this.zoom = 11;
      this.centerX = lngToWorldX(129.0756, 11);
      this.centerY = latToWorldY(35.1796, 11);
      return;
    }
    var lats = pts.map(function (p) { return p.lat; });
    var lngs = pts.map(function (p) { return p.lng; });
    var latMid = (Math.min.apply(null, lats) + Math.max.apply(null, lats)) / 2;
    var lngMid = (Math.min.apply(null, lngs) + Math.max.apply(null, lngs)) / 2;

    var w = this.el.clientWidth || 600;
    var h = this.el.clientHeight || 320;
    var z = this.maxZoom;
    for (; z > this.minZoom; z--) {
      var xs = lngs.map(function (v) { return lngToWorldX(v, z); });
      var ys = lats.map(function (v) { return latToWorldY(v, z); });
      var dx = (Math.max.apply(null, xs) - Math.min.apply(null, xs)) * TILE;
      var dy = (Math.max.apply(null, ys) - Math.min.apply(null, ys)) * TILE;
      // 가장자리에 지점이 붙지 않도록 여백을 둔다.
      if (dx < w * 0.82 && dy < h * 0.82) { break; }
    }
    this.zoom = z;
    this.centerX = lngToWorldX(lngMid, z);
    this.centerY = latToWorldY(latMid, z);
  };

  UGMap.prototype.zoomBy = function (delta) {
    var z = Math.max(this.minZoom, Math.min(this.maxZoom, this.zoom + delta));
    if (z === this.zoom) { return; }
    var scale = Math.pow(2, z - this.zoom);
    this.centerX *= scale;
    this.centerY *= scale;
    this.zoom = z;
    this.render();
  };

  UGMap.prototype._url = function (x, y, z) {
    return this.tileUrl
      .replace('{z}', z).replace('{x}', x).replace('{y}', y);
  };

  UGMap.prototype.render = function () {
    var self = this;
    var w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) { return; }
    var n = Math.pow(2, this.zoom);

    // 화면 왼쪽 위가 세계 좌표 어디인지
    var originX = this.centerX - w / (2 * TILE);
    var originY = this.centerY - h / (2 * TILE);

    var x0 = Math.floor(originX), y0 = Math.floor(originY);
    var x1 = Math.floor(originX + w / TILE), y1 = Math.floor(originY + h / TILE);

    var keep = {};
    for (var x = x0; x <= x1; x++) {
      for (var y = y0; y <= y1; y++) {
        if (y < 0 || y >= n) { continue; }           // 위아래 밖은 타일이 없다
        var wx = ((x % n) + n) % n;                  // 좌우는 감는다
        var key = this.zoom + '/' + wx + '/' + y;
        keep[key] = true;
        if (!this._tiles) { this._tiles = {}; }
        var img = this._tiles[key];
        if (!img) {
          img = document.createElement('img');
          img.alt = '';
          // **loading="lazy" 를 쓰지 않는다.** 타일은 절대 위치로 깔려 있어
          // 브라우저의 지연 로딩 판단이 지도 화면과 맞지 않는다. 실제로
          // 6장 중 1장만 받아 놓고 나머지를 무한정 미루는 것을 확인했다.
          img.decoding = 'async';
          img.src = this._url(wx, y, this.zoom);
          this.tilesTried++;
          img.addEventListener('load', function () {
            self.tilesLoaded++;
            clearTimeout(self._deadline);   // 한 장이라도 왔으면 지도는 산다
          });
          img.addEventListener('error', function () {
            self.tilesFailed++;
            // 하나도 못 받으면 배경지도가 아예 없는 것이다.
            if (!self.failReported && self.tilesFailed >= self.tilesTried
                && self.tilesTried >= 3) {
              self.failReported = true;
              self.onFail();
            }
          });
          this._tiles[key] = img;
          this.pane.appendChild(img);
        }
        img.style.left = ((x - originX) * TILE) + 'px';
        img.style.top = ((y - originY) * TILE) + 'px';
      }
    }
    // 화면 밖 타일은 버린다. 오래 켜 두면 <img> 가 계속 쌓인다.
    Object.keys(this._tiles || {}).forEach(function (key) {
      if (!keep[key]) {
        self._tiles[key].remove();
        delete self._tiles[key];
      }
    });

    this._renderMarkers(originX, originY);
  };

  UGMap.prototype._renderMarkers = function (originX, originY) {
    var self = this;
    this.markerPane.innerHTML = '';
    this.points.forEach(function (p) {
      if (typeof p.lat !== 'number' || typeof p.lng !== 'number') { return; }
      var px = (lngToWorldX(p.lng, self.zoom) - originX) * TILE;
      var py = (latToWorldY(p.lat, self.zoom) - originY) * TILE;
      var m = document.createElement(p.href ? 'a' : 'span');
      m.className = 'ug-tilemap-pin';
      if (p.href) { m.href = p.href; }
      m.style.left = px + 'px';
      m.style.top = py + 'px';
      m.style.background = p.color || '#3fb950';
      m.title = p.title || p.name || '';
      m.setAttribute('aria-label', m.title);
      self.markerPane.appendChild(m);
    });
  };

  global.UGMap = UGMap;
}(window));
