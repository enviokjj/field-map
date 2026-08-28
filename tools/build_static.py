"""build_static.py — 서버 없이 도는 **정적 번들**을 굽는다 (GitHub Pages 등).

왜 이게 되나
------------
이 앱이 서버에 요구하는 것은 **읽기 전용 GET 3종**뿐이다:

    GET /tiles/layers                       (작은 JSON)
    GET /tiles/road_line/{z}/{x}/{y}.pbf    (도로 벡터 타일)
    GET /boundary/layers · /boundary/{layer}/items   (경계 GeoJSON)

전부 "같은 주소에 같은 응답"이라 **파일로 구워 두면 그대로 대체된다**. 쿼리스트링
(`?bbox=…`)은 정적 호스팅에서 무시되는데, 굽는 쪽이 이미 지역 범위로 잘라 두므로
그 무시가 오히려 맞는 동작이다(한 번에 다 받고 다시 안 받는다).

용량 (인제군 z12~16, 실측 표본 추정)
    z12 63타일 4.8MB · z13 234 4.4MB · z14 884 3.9MB · z15 3,417 2.4MB · z16 13,400 4.6MB
    → 도로 약 20MB + 경계 약 2.5MB + 페이지·폰트 0.1MB ≈ **23MB**
  GitHub Pages 한도(사이트 1GB · 파일 100MB)에 여유롭게 들어간다.

★빈 타일은 안 쓴다 — 도로가 없는 산지 타일이 절반을 넘는다. 파일 수와 용량이 크게 준다.
  maplibre 는 404 를 '빈 타일'로 조용히 처리한다.

사용
    python tools/build_static.py                       # .env 의 DB 로 인제군 굽기
    python tools/build_static.py --region adm_sigungu:5181000000
    python tools/build_static.py --zoom 12 16 --out docs
    python tools/build_static.py --page-only        # 페이지만 고쳤을 때 (DB 불필요, 몇 초)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import shutil
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

LAYER_LABELS = {"adm_sido": "시도", "adm_sigungu": "시군구", "adm_emd": "읍면동", "adm_ri": "리",
                "basin_l": "대권역", "basin_m": "중권역", "basin_s": "표준유역"}
# 전국을 그대로 구워도 되는 레이어(작다). 나머지는 지역 범위로 자른다.
NATIONWIDE = {"adm_sido", "adm_sigungu", "basin_l", "basin_m"}

TILE_SQL = text("""
    WITH env AS (
      SELECT ST_TileEnvelope(:z,:x,:y) g3857, ST_Transform(ST_TileEnvelope(:z,:x,:y),5186) g5186
    ), src AS (
      SELECT r.geom, "명칭" AS name FROM terrain.road_line r, env
      WHERE r.geom && env.g5186 LIMIT 60000        -- ★5186 끼리 비교해야 GIST 인덱스를 탄다
    ), m AS (
      SELECT ST_AsMVTGeom(ST_Transform(src.geom,3857), env.g3857, 4096, 64, true) geom, name
      FROM src, env
    )
    SELECT ST_AsMVT(m,'road_line',4096,'geom') FROM m""")


def tile_xy(lon, lat, z):
    n = 2 ** z
    return (int((lon + 180) / 360 * n),
            int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
                 / math.pi) / 2 * n))


def region_bbox(con, layer, code):
    row = con.execute(text("""
        SELECT b.name, ST_XMin(g) x1, ST_YMin(g) y1, ST_XMax(g) x2, ST_YMax(g) y2
        FROM terrain.boundary b, LATERAL (SELECT ST_Transform(b.bbox,4326) g) t
        WHERE b.layer=:l AND b.code=:c"""), {"l": layer, "c": code}).mappings().first()
    if not row:
        raise SystemExit(f"연구지역을 못 찾았다: {layer}/{code}")
    return row["name"], (row["x1"], row["y1"], row["x2"], row["y2"])


def boundary_items(con, layer, bbox=None):
    where, params = "WHERE b.layer=:layer", {"layer": layer}
    if bbox:
        params |= dict(zip(("x1", "y1", "x2", "y2"), bbox))
        where += " AND b.geom && ST_Transform(ST_MakeEnvelope(:x1,:y1,:x2,:y2,4326),5186)"
    return con.execute(text(f"""
        SELECT jsonb_build_object('type','FeatureCollection',
          'features', coalesce(jsonb_agg(f ORDER BY ord),'[]'::jsonb))::text
        FROM (
          SELECT row_number() OVER (ORDER BY b.area_km2 DESC) ord,
                 jsonb_build_object('type','Feature','id',b.code,
                   'geometry', ST_AsGeoJSON(ST_Transform(b.geom,4326),5)::jsonb,
                   'properties', jsonb_build_object(
                     'layer',b.layer,'code',b.code,'name',b.name,'parent_name',p.name,
                     'area_km2', round(b.area_km2::numeric,1),
                     -- 라벨 대표점 = **최대 파트의 ST_PointOnSurface**(중심점이 아니다 —
                     -- 도넛 모양이면 중심점이 구멍 안에 떨어진다)
                     'lon', round(ST_X(lbl.pt)::numeric,5),
                     'lat', round(ST_Y(lbl.pt)::numeric,5))) f
          FROM terrain.boundary b
          LEFT JOIN terrain.boundary p ON p.layer=b.parent_layer AND p.code=b.parent_code
          CROSS JOIN LATERAL (
            SELECT ST_Transform(ST_PointOnSurface(d.geom),4326) pt
            FROM (SELECT (ST_Dump(b.geom)).geom FROM terrain.boundary bb WHERE bb.code=b.code AND bb.layer=b.layer) d
            ORDER BY ST_Area(d.geom) DESC LIMIT 1) lbl
          {where}) s"""), params).scalar()


def main(argv=None):
    ap = argparse.ArgumentParser(description="정적 번들 굽기")
    ap.add_argument("--region", default="adm_sigungu:5181000000", help="layer:code (기본 인제군)")
    ap.add_argument("--zoom", nargs=2, type=int, default=[12, 16], metavar=("MIN", "MAX"))
    ap.add_argument("--out", default="docs")   # GitHub Pages 의 main/docs 를 그대로 쓴다
    ap.add_argument("--extent", choices=["aoi", "region"], default="aoi",
                    help="굽는 범위 — aoi=연구지역만(기본) · region=시군구 전체")
    ap.add_argument("--pad", type=float, default=0.01,
                    help="범위를 이만큼(도) 넓혀 굽는다 — 가장자리가 휑하지 않게")
    ap.add_argument("--aoi", default="인제 훈련",
                    help="담을 연구지역 이름(쉼표 구분). 빈 값이면 범위 안 전부")
    ap.add_argument("--page-only", action="store_true",
                    help="페이지·폰트만 web/ → out/ 으로 복사한다. DB 불필요, 타일·경계는 그대로 둔다")
    a = ap.parse_args(argv)

    # ★페이지만 고쳤을 때 쓰는 길.
    #   web/index.html 을 고쳐도 docs/ 는 굽기 산출물이라 **안 바뀐다** — 그대로 push 하면
    #   사이트가 그대로다. 그렇다고 데이터까지 다시 구우면 DB 가 필요하고 오래 걸린다.
    #   이 옵션이 그 사이를 메운다(복사만, 몇 초).
    if a.page_only:
        out = (ROOT / a.out).resolve()
        if not out.is_dir():
            raise SystemExit(f"{out} 가 없다 — 처음에는 --page-only 없이 전체를 구울 것")
        import filecmp
        changed = []
        for src in (ROOT / "web").rglob("*"):
            if not src.is_file():
                continue
            dst = out / src.relative_to(ROOT / "web")
            same = dst.exists() and filecmp.cmp(src, dst, shallow=False)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if not same:
                changed.append(str(dst.relative_to(out)))
        print(f"페이지 복사 → {out}")
        for c in changed:
            print(f"  갱신: {c}")
        if not changed:
            print("  (바뀐 파일 없음)")
        print("\n  타일·경계는 그대로 둔다. 데이터까지 갱신하려면 --page-only 없이 다시 구울 것.")
        return 0

    db = os.environ.get("DB_URL")
    if not db:
        raise SystemExit("DB_URL 이 없다 — .env.example 를 .env 로 복사해 채울 것")
    eng = create_engine(db)
    layer, _, code = a.region.partition(":")
    out = (ROOT / a.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    with eng.connect() as con:
        # ★굽는 범위 = **연구지역(AOI) 자체**. 종전엔 시군구 전체를 구웠는데
        #   (인제군 0.65°×0.67°) 실제로 필요한 건 AOI(0.19°×0.13°)뿐이라 17배를 낭비했다.
        #   `--extent region` 을 주면 종전처럼 시군구 전체를 굽는다.
        names = [x.strip() for x in a.aoi.split(",") if x.strip()]
        if a.extent == "aoi" and names:
            row = con.execute(text("""
                -- ★먼저 4326 으로 옮긴 뒤 범위를 잡는다. ST_Extent 는 box2d 라
                --   그대로 geometry 로 캐스팅하면 SRID 를 잃고 ST_Transform 이 터진다.
                SELECT ST_XMin(g) x1, ST_YMin(g) y1, ST_XMax(g) x2, ST_YMax(g) y2
                FROM (SELECT ST_Extent(ST_Transform(fp, 4326)) g FROM (
                        SELECT DISTINCT ON (a.properties->>'name') a.footprint fp
                        FROM catalog.asset a
                        WHERE a.kind='aoi_cube' AND a.properties->>'name' = ANY(:names)
                        ORDER BY a.properties->>'name', a.acquired_at DESC) t) u"""),
                {"names": names}).mappings().first()
            if not row or row["x1"] is None:
                raise SystemExit(f"연구지역을 못 찾았다: {names}")
            name = ", ".join(names)
            bb = (row["x1"], row["y1"], row["x2"], row["y2"])
        else:
            name, bb = region_bbox(con, layer, code)
        W, S, E, N = bb[0] - a.pad, bb[1] - a.pad, bb[2] + a.pad, bb[3] + a.pad
        print(f"굽는 범위 [{a.extent}] {name}  bbox {W:.4f},{S:.4f},{E:.4f},{N:.4f}"
              f"  ({(E-W):.3f}° × {(N-S):.3f}°)")

        # ── 페이지·폰트 ──────────────────────────────────────────────────────
        shutil.copytree(ROOT / "web", out, dirs_exist_ok=True)
        (out / ".nojekyll").write_text("")        # GitHub Pages 가 _ 로 시작하는 경로를 안 지우게

        # ── 도로 타일 ────────────────────────────────────────────────────────
        (out / "tiles").mkdir(parents=True, exist_ok=True)
        (out / "tiles" / "layers").write_text(json.dumps(
            {"layers": [{"layer": "road_line", "minzoom": a.zoom[0]}], "max_zoom": 22},
            ensure_ascii=False))
        total_b = n_written = n_empty = 0
        for z in range(a.zoom[0], a.zoom[1] + 1):
            x1, y1 = tile_xy(W, N, z)
            x2, y2 = tile_xy(E, S, z)
            zb = zn = 0
            for x in range(x1, x2 + 1):
                for y in range(y1, y2 + 1):
                    data = con.execute(TILE_SQL, {"z": z, "x": x, "y": y}).scalar()
                    if not data:
                        n_empty += 1
                        continue          # ★빈 타일은 안 쓴다 — maplibre 는 404 를 빈 타일로 본다
                    p = out / "tiles" / "road_line" / str(z) / str(x)
                    p.mkdir(parents=True, exist_ok=True)
                    (p / f"{y}.pbf").write_bytes(bytes(data))
                    zb += len(data); zn += 1
            total_b += zb; n_written += zn
            print(f"  z{z}: {zn:>6,} 타일 {zb/1e6:>6.1f}MB  (빈 타일 제외)")

        # ── 연구지역(AOI) — 서버에 실제로 등록된 것 ──────────────────────────
        #   catalog.asset(kind='aoi_cube') 의 footprint 가 그 연구지역의 범위다.
        #   ★같은 이름으로 여러 번 구축된 것이 있다 — **이름별 최신 하나**만 쓴다
        #     (인제는 '인제 훈련' 이 3번, '인제_test2' 가 5번 있었다).
        aoi = con.execute(text("""
            SELECT jsonb_build_object('type','FeatureCollection',
              'features', coalesce(jsonb_agg(f ORDER BY nm), '[]'::jsonb))::text
            FROM (
              SELECT nm, jsonb_build_object('type','Feature',
                'geometry', ST_AsGeoJSON(ST_Transform(fp, 4326), 6)::jsonb,
                'properties', jsonb_build_object(
                  'name', nm, 'km2', round((ST_Area(fp)/1e6)::numeric, 1),
                  'built', to_char(at, 'YYYY-MM-DD'))) AS f
              FROM (
                SELECT DISTINCT ON (a.properties->>'name')
                       a.properties->>'name' AS nm, a.footprint AS fp, a.acquired_at AS at
                FROM catalog.asset a
                WHERE a.kind='aoi_cube' AND a.properties->>'name' IS NOT NULL
                  AND (:names IS NULL OR a.properties->>'name' = ANY(:names))
                ORDER BY a.properties->>'name', a.acquired_at DESC
              ) t
            ) s"""), {"x1": W, "y1": S, "x2": E, "y2": N,
              "names": [x.strip() for x in a.aoi.split(",") if x.strip()] or None}).scalar()
        (out / "aoi").mkdir(parents=True, exist_ok=True)
        (out / "aoi" / "items").write_text(aoi, encoding="utf8")
        aoi_n = json.loads(aoi)["features"]
        print(f"  연구지역(AOI) {len(aoi_n)}개 {len(aoi)/1e6:.3f}MB — "
              + ", ".join(f"{x['properties']['name']}({x['properties']['km2']}km²)" for x in aoi_n))

        # ── 리(법정리)만 굽는다 ──────────────────────────────────────────────
        #   '연구지역 선택' 도구는 뺐다(AOI 를 그대로 보여주면 되므로). 대신 **리 지명은
        #   현장에서 항상 필요**하다는 요구가 있어 그 한 겹만 남긴다.
        #   시도·시군구·읍면동·유역은 굽지 않는다 — 4.8MB → 0.6MB.
        (out / "boundary" / "adm_ri").mkdir(parents=True, exist_ok=True)
        gj = boundary_items(con, "adm_ri", (W, S, E, N))
        (out / "boundary" / "adm_ri" / "items").write_text(gj, encoding="utf8")
        bnd_b = len(gj.encode())
        print(f"  리 {len(json.loads(gj)['features']):,}건 {bnd_b/1e6:.2f}MB (지역)")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    files = sum(1 for f in out.rglob("*") if f.is_file())
    print(f"\n★ {out}")
    print(f"  도로 타일 {n_written:,}개 {total_b/1e6:.1f}MB (빈 타일 {n_empty:,}개 생략)")
    print(f"  전체 {files:,}파일 {size/1e6:.1f}MB")
    if size > 900e6:
        print("  ⚠ GitHub Pages 사이트 한도(1GB)에 근접한다 — 줌 범위나 지역을 줄일 것")
    print("\n  다음: 이 폴더를 git 저장소 루트(또는 docs/)에 두고 Pages 를 켠다.")
    print("        ★V-World 인증키는 **도메인에 묶인다** — vworld.kr 에 "
          "`<계정>.github.io` 를 등록해야 배경 타일이 온다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
