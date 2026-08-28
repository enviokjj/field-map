/* 현장 지도 — 오프라인 저장소(서비스 워커).
 *
 * 왜 필요한가
 * ----------
 * "인터넷 되는 곳에서 미리 켜두면 되지 않나" 는 반은 맞다. 탭을 켠 채로 끊기면 쓸 수 있다.
 * 그런데 새로고침하거나 폰이 탭을 정리하면 끝이다 — 실측한 캐시 수명이
 *   GitHub Pages 의 index.html·roads.geojson : max-age=600  (10분)
 * 이라, 10분만 지나면 페이지 자체를 다시 받아야 하는데 그게 안 된다.
 * 서비스 워커는 그 파일들을 10분짜리가 아니라 **영구 저장소**에 넣는다.
 *
 * 규칙 — **온라인이면 항상 온라인, 끊기면 오프라인**
 * ------------------------------------------------
 *   우리 파일·브이월드  : 네트워크 먼저, 실패하면 캐시
 *   배경지도 타일(79MB) : 캐시 먼저 (한 번 받으면 안 바뀐다 — 매번 받을 이유가 없다)
 *
 * ★한때 우리 파일을 캐시 먼저로 뒀는데, 그러면 온라인인데도 **옛 파일이 나온다**.
 *   기본은 온라인이다. 네트워크가 살아 있으면 언제나 그쪽을 쓴다.
 *   (GitHub Pages 가 max-age=600 을 주므로 브라우저 HTTP 캐시가 중복 왕복은 막아 준다)
 *
 * ★배경지도 z8~16 은 번들에 들어 있지만(docs/basemap), **본 적 없는 타일은 캐시에 없다**.
 *   화면의 '오프라인 준비' 가 manifest.json 을 읽어 5,892장을 한 번에 저장한다.
 */
/* ★캐시를 **둘로 나눈다**. 하나로 두면 index.html 을 한 줄만 고쳐도 판이 바뀌어
   배경지도 79MB 를 통째로 다시 받게 된다.
     앱   — 페이지·도로·경계 (약 5.6MB). 내용이 바뀌면 판이 올라가 **항상 최신**이 된다.
     타일 — 배경지도 79MB. 목록(manifest)이 바뀔 때만 판이 올라간다.
   두 판 모두 build_static.py 가 **내용 해시로** 박는다(사람이 올리면 잊는다). */
const VERSION = "20b46146d0";          // 앱·데이터 내용 해시
const TILEVER = "74d224873d";          // 배경지도 목록 해시
const CACHE = "fieldmap-app-" + VERSION;
const TILES = "fieldmap-tiles-" + TILEVER;
const BASE = new URL("./", self.location).pathname;      // 예: /field-map/
const isTile = (p) => p.startsWith(BASE + "basemap/");

/* 페이지가 뜨는 데 반드시 있어야 하는 것들 — 설치할 때 미리 받는다(약 5.6MB). */
const SHELL = ["", "index.html", "sw.js",
               "vendor/maplibre-gl.js", "vendor/maplibre-gl.css",
               "roads.geojson", "aoi/items", "boundary/adm_ri/items",
               "fonts/Noto Sans Regular/0-255.pbf"];

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    // ★하나가 실패해도 설치를 통째로 실패시키지 않는다 — addAll 은 전부-아니면-전무다.
    await Promise.all(SHELL.map(p =>
      c.add(new Request(BASE + p, {cache: "reload"})).catch(() => {})));
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys())
      if (k !== CACHE && k !== TILES) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // 주소창으로 들어온 요청(새로고침 포함) — 오프라인이면 저장해 둔 페이지를 준다
  if (req.mode === "navigate") {
    e.respondWith((async () => {
      try { return await fetch(req); }
      catch (err) {
        const c = await caches.open(CACHE);
        return (await c.match(BASE + "index.html")) || (await c.match(BASE)) ||
               new Response("오프라인입니다", {status: 503});
      }
    })());
    return;
  }

  const mine = url.origin === self.location.origin;
  if (!mine && url.hostname !== "api.vworld.kr") return;   // 그 밖은 손대지 않는다

  // 배경지도 타일만 캐시 먼저 — 79MB 를 매번 다시 받을 이유가 없다
  if (mine && isTile(url.pathname)) {
    e.respondWith((async () => {
      const c = await caches.open(TILES);
      return (await c.match(req)) || fetch(req);
    })());
    return;
  }

  // 나머지는 전부 **네트워크 먼저**. 끊겼을 때만 저장해 둔 것을 쓴다.
  e.respondWith((async () => {
    const c = await caches.open(mine ? CACHE : TILES);
    try {
      const res = await fetch(req);
      if (res && res.ok) c.put(req, res.clone());
      return res;
    } catch (err) {
      return (await c.match(req)) || Response.error();
    }
  })());
});

/* '오프라인 준비' — 배경지도 5,892장을 한 번에 저장하고 진행률을 알려 준다. */
self.addEventListener("message", (e) => {
  if (!e.data || e.data.type !== "BAKE") return;
  e.waitUntil((async () => {
    const send = (m) => e.source && e.source.postMessage(m);
    const c = await caches.open(TILES);          // 배경지도는 타일 캐시에 담는다
    let list = [], remote = "";
    try {
      const r = await fetch(BASE + "basemap/manifest.json", {cache: "reload"});
      const m = await r.json();
      list = m.tiles || []; remote = m.remote || "";
    } catch (err) { return send({type: "BAKE_DONE", ok: 0, total: 0, err: "목록을 못 읽었다"}); }
    /* basemap/Base/16/25290/56084.png → 화면이 실제로 부르는 **브이월드 주소**.
       ★열쇠를 이렇게 맞춰야 오프라인에서 같은 요청이 캐시에 걸린다. 번들 경로로만
         저장하면 화면은 브이월드를 부르므로 저장해 둔 것이 **쓰이지 않는다**. */
    function remoteUrl(p) {
      const m = /^basemap\/([^/]+)\/(\d+)\/(\d+)\/(\d+)\.(\w+)$/.exec(p);
      if (!m || !remote) return null;
      return remote.replace("{layer}", m[1]).replace("{z}", m[2])
                   .replace("{y}", m[3]).replace("{x}", m[4]).replace("{ext}", m[5]);
    }
    let ok = 0, fail = 0;
    const LANES = 6;                     // 폰에서 너무 많이 열면 오히려 느려진다
    let i = 0;
    async function lane() {
      while (i < list.length) {
        const rel = list[i++];
        const key = remoteUrl(rel);
        if (!key) { ok++; continue; }                 // manifest.json 자신 등
        try {
          if (!(await c.match(key))) {
            const res = await fetch(BASE + rel);      // 번들에서 받아
            if (res && res.ok) await c.put(key, res.clone());   // 원본 주소로 저장
            else fail++;
          }
          ok++;
        } catch (err) { fail++; }
        if (ok % 100 === 0) send({type: "BAKE_PROGRESS", ok, total: list.length});
      }
    }
    await Promise.all(Array.from({length: LANES}, lane));
    send({type: "BAKE_DONE", ok, total: list.length, fail});
  })());
});
