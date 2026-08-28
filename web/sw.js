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
 * 두 가지 규칙
 * -----------
 *   같은 출처(우리 파일)  : 캐시 먼저 — 오프라인에서도 즉시 뜬다
 *   브이월드(z16 위 원본) : 네트워크 먼저, 실패하면 캐시 — 온라인에선 항상 최신
 *
 * ★배경지도 z8~16 은 번들에 들어 있지만(docs/basemap), **본 적 없는 타일은 캐시에 없다**.
 *   화면의 '오프라인 준비' 가 manifest.json 을 읽어 5,892장을 한 번에 저장한다.
 */
/* ★캐시를 **둘로 나눈다**. 하나로 두면 index.html 을 한 줄만 고쳐도 판이 바뀌어
   배경지도 79MB 를 통째로 다시 받게 된다.
     앱   — 페이지·도로·경계 (약 5.6MB). 내용이 바뀌면 판이 올라가 **항상 최신**이 된다.
     타일 — 배경지도 79MB. 목록(manifest)이 바뀔 때만 판이 올라간다.
   두 판 모두 build_static.py 가 **내용 해시로** 박는다(사람이 올리면 잊는다). */
const VERSION = "__VERSION__";          // 앱·데이터 내용 해시
const TILEVER = "__TILEVER__";          // 배경지도 목록 해시
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

  if (url.origin === self.location.origin) {          // 우리 파일 — 캐시 먼저
    e.respondWith((async () => {
      const c = await caches.open(isTile(url.pathname) ? TILES : CACHE);
      const hit = await c.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res && res.ok) c.put(req, res.clone());
      return res;
    })());
    return;
  }

  if (url.hostname === "api.vworld.kr") {             // 배경지도 원본 — 네트워크 먼저
    e.respondWith((async () => {
      const c = await caches.open(CACHE);
      try {
        const res = await fetch(req);
        if (res && res.ok) c.put(req, res.clone());
        return res;
      } catch (err) {
        return (await c.match(req)) || Response.error();
      }
    })());
  }
});

/* '오프라인 준비' — 배경지도 5,892장을 한 번에 저장하고 진행률을 알려 준다. */
self.addEventListener("message", (e) => {
  if (!e.data || e.data.type !== "BAKE") return;
  e.waitUntil((async () => {
    const send = (m) => e.source && e.source.postMessage(m);
    const c = await caches.open(TILES);          // 배경지도는 타일 캐시에 담는다
    let list = [];
    try {
      const r = await fetch(BASE + "basemap/manifest.json", {cache: "reload"});
      list = (await r.json()).tiles || [];
    } catch (err) { return send({type: "BAKE_DONE", ok: 0, total: 0, err: "목록을 못 읽었다"}); }
    let ok = 0, fail = 0;
    const LANES = 6;                     // 폰에서 너무 많이 열면 오히려 느려진다
    let i = 0;
    async function lane() {
      while (i < list.length) {
        const p = BASE + list[i++];
        try {
          if (!(await c.match(p))) {
            const res = await fetch(p);
            if (res && res.ok) await c.put(p, res.clone()); else fail++;
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
