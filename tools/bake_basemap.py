#!/usr/bin/env python3
"""배경지도 타일을 **미리 받아** 번들에 넣는다 — 인터넷 없이 쓰기 위해.

왜
--
현장에서 인터넷이 끊기면 배경지도가 통째로 회색이 된다. 도로·리·연구지역은 이미
파일로 들어 있어 괜찮지만 배경만 비었다. 그래서 연구지역 범위만 미리 굽는다.

무엇을
------
z8~16 · Base(브이월드) · Satellite(위성) · Hybrid(위성 지명). 실측 평균 타일 크기는
Base 13.4KB · Satellite 16.2KB · Hybrid 3.4KB 다.

★z16 까지만 굽는다. 그 위는 z16 타일을 확대해 쓴다 — 배경 사진만 부드러워지고
  **도로·리 이름은 벡터라 계속 날카롭다**. z17 을 더 구우면 위성만 +91MB 다.
★인터넷이 되는 동안에는 z16 위에서 브이월드 원본을 겹쳐 선명하게 본다(화면 쪽 처리).

멱등하다 — 이미 받은 타일은 건너뛴다(중간에 끊겨도 다시 돌리면 이어서 받는다).

    python tools/bake_basemap.py                 # docs/aoi/items 의 범위로
    python tools/bake_basemap.py --zoom 8 17     # 더 선명하게(용량 급증)
"""
import argparse
import concurrent.futures as cf
import io
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAYERS = [("Base", "png"), ("Satellite", "jpeg"), ("Hybrid", "png")]
# ★키는 **도메인에 묶인다**. 서버에서 받을 때는 Referer 를 붙여야 통과한다.
REFERER = "https://enviokjj.github.io/"


def tile_xy(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat = max(min(lat, 85.05), -85.05)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def read_key():
    """web/index.html 의 FIELD_CFG.vworldKey 를 정본으로 쓴다(사본을 만들지 않는다)."""
    m = re.search(r'vworldKey:\s*"([^"]+)"', (ROOT / "web" / "index.html").read_text(encoding="utf8"))
    return os.environ.get("VWORLD_KEY") or (m.group(1) if m else None)


def aoi_bbox(out):
    """굽는 범위 = **이미 구워 둔 AOI**. DB 를 다시 붙지 않는다(사본이 갈라지지 않게)."""
    p = out / "aoi" / "items"
    if not p.is_file():
        sys.exit(f"{p} 가 없다 — build_static.py 를 먼저 돌릴 것")
    gj = json.loads(p.read_text(encoding="utf8"))
    xs = [c[0] for f in gj["features"] for r in f["geometry"]["coordinates"] for c in r]
    ys = [c[1] for f in gj["features"] for r in f["geometry"]["coordinates"] for c in r]
    return min(xs), min(ys), max(xs), max(ys)


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Referer": REFERER,
                                                       "User-Agent": "field-map/bake"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None                       # 그 자리에 타일이 없다 — 정상
            last = e
        except Exception as e:                    # noqa: BLE001
            last = e
        time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"{url} — {last}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="배경지도 타일 미리 굽기")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--zoom", nargs=2, type=int, default=[8, 16], metavar=("MIN", "MAX"))
    ap.add_argument("--pad", type=float, default=0.01, help="범위를 이만큼(도) 넓힌다")
    ap.add_argument("--layers", default="Base,Satellite,Hybrid")
    ap.add_argument("--workers", type=int, default=8, help="동시 내려받기 수")
    ap.add_argument("--dry-run", action="store_true", help="장 수·예상 용량만 센다")
    a = ap.parse_args(argv)

    out = (ROOT / a.out).resolve()
    key = read_key()
    if not key:
        sys.exit("V-World 키를 못 찾았다 — web/index.html 의 FIELD_CFG.vworldKey 또는 VWORLD_KEY")
    want = [(n, e) for n, e in LAYERS if n in {x.strip() for x in a.layers.split(",")}]
    W, S, E, N = aoi_bbox(out)
    W, S, E, N = W - a.pad, S - a.pad, E + a.pad, N + a.pad
    print(f"굽는 범위 {W:.4f},{S:.4f},{E:.4f},{N:.4f}  z{a.zoom[0]}~{a.zoom[1]}  "
          f"레이어 {[n for n, _ in want]}")

    jobs = []
    for name, ext in want:
        for z in range(a.zoom[0], a.zoom[1] + 1):
            x1, y1 = tile_xy(W, N, z)
            x2, y2 = tile_xy(E, S, z)
            for x in range(x1, x2 + 1):
                for y in range(y1, y2 + 1):
                    dst = out / "basemap" / name / str(z) / str(y) / f"{x}.{ext}"
                    url = f"https://api.vworld.kr/req/wmts/1.0.0/{key}/{name}/{z}/{y}/{x}.{ext}"
                    jobs.append((dst, url))
    have = sum(1 for d, _ in jobs if d.is_file())
    print(f"  대상 {len(jobs):,}장 (이미 받은 것 {have:,}장은 건너뛴다)")
    if a.dry_run:
        avg = {"Base": 13.4, "Satellite": 16.2, "Hybrid": 3.4}
        est = sum(avg[n] for n, _ in want for _ in range(1)) and sum(
            avg[d.parts[-4]] for d, _ in jobs) / 1024
        print(f"  예상 용량 ≈ {est:.0f}MB")
        return 0

    todo = [(d, u) for d, u in jobs if not d.is_file()]
    n_ok = n_empty = n_bytes = 0
    t0 = time.time()

    def one(item):
        dst, url = item
        data = fetch(url)
        if data is None or len(data) < 100:
            return 0
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return len(data)

    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, got in enumerate(ex.map(one, todo), 1):
            if got:
                n_ok += 1
                n_bytes += got
            else:
                n_empty += 1
            if i % 500 == 0 or i == len(todo):
                print(f"    {i:,}/{len(todo):,}  {n_bytes/1e6:.1f}MB  "
                      f"{i/max(time.time()-t0,0.1):.0f}장/s")

    # ── 목록 — 화면의 '오프라인 준비' 가 이걸 읽어 한 번에 저장한다 ──────────
    files = sorted(str(p.relative_to(out)).replace(os.sep, "/")
                   for p in (out / "basemap").rglob("*") if p.is_file())
    total = sum((out / f).stat().st_size for f in files)
    (out / "basemap" / "manifest.json").write_text(json.dumps(
        {"tiles": files, "bytes": total, "zoom": a.zoom, "layers": [n for n, _ in want]},
        ensure_ascii=False), encoding="utf8")
    per = {}
    for f in files:
        per[f.split("/")[1]] = per.get(f.split("/")[1], 0) + (out / f).stat().st_size
    print(f"\n★ 배경지도 {len(files):,}장 {total/1e6:.1f}MB")
    for k, v in sorted(per.items()):
        print(f"    {k:<10} {v/1e6:6.1f}MB")
    print(f"    (빈 자리 {n_empty:,}장)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
