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
    ap.add_argument("--pad", type=float, default=0.05, help="지역 bbox 를 이만큼(도) 넓혀 굽는다")
    a = ap.parse_args(argv)

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
        name, bb = region_bbox(con, layer, code)
        W, S, E, N = bb[0] - a.pad, bb[1] - a.pad, bb[2] + a.pad, bb[3] + a.pad
        print(f"연구지역 {name} ({layer}:{code})  bbox {W:.4f},{S:.4f},{E:.4f},{N:.4f}")

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

        # ── 경계 ─────────────────────────────────────────────────────────────
        (out / "boundary").mkdir(parents=True, exist_ok=True)
        groups = {"adm": [], "water": []}
        bnd_b = 0
        for ly, label in LAYER_LABELS.items():
            gj = boundary_items(con, ly, None if ly in NATIONWIDE else (W, S, E, N))
            d = out / "boundary" / ly
            d.mkdir(parents=True, exist_ok=True)
            (d / "items").write_text(gj, encoding="utf8")
            bnd_b += len(gj.encode())
            n = json.loads(gj)["features"]
            # ★needs_bbox=false 로 굽는다. 이미 지역 범위로 잘라 놨으니 화면이 이동할 때마다
            #   다시 받을 이유가 없다(정적이라 어차피 같은 파일이 온다).
            (groups["adm"] if ly.startswith("adm_") else groups["water"]).append(
                {"layer": ly, "label": label, "count": len(n), "needs_bbox": False})
            print(f"  {label:<7} {len(n):>6,}건 {len(gj)/1e6:>5.2f}MB"
                  + ("  (전국)" if ly in NATIONWIDE else "  (지역)"))
        (out / "boundary" / "layers").write_text(json.dumps(
            {"groups": [{"group": "adm", "label": "행정구역", "layers": groups["adm"]},
                        {"group": "water", "label": "수자원 단위지도", "layers": groups["water"]}]},
            ensure_ascii=False), encoding="utf8")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    files = sum(1 for f in out.rglob("*") if f.is_file())
    print(f"\n★ {out}")
    print(f"  도로 타일 {n_written:,}개 {total_b/1e6:.1f}MB (빈 타일 {n_empty:,}개 생략)")
    print(f"  경계 {bnd_b/1e6:.1f}MB · 전체 {files:,}파일 {size/1e6:.1f}MB")
    if size > 900e6:
        print("  ⚠ GitHub Pages 사이트 한도(1GB)에 근접한다 — 줌 범위나 지역을 줄일 것")
    print("\n  다음: 이 폴더를 git 저장소 루트(또는 docs/)에 두고 Pages 를 켠다.")
    print("        ★V-World 인증키는 **도메인에 묶인다** — vworld.kr 에 "
          "`<계정>.github.io` 를 등록해야 배경 타일이 온다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
