"""현장 지도 서버 — 자립형 최소 게이트웨이.

무엇인가
--------
`spatial_analysis`(지형분석 통합 서버)의 **구축 화면에서 네 가지만** 떼어낸 독립 앱이다:
연구지역 선택 · 배경지도 선택 · 도로(중심선) · GPS 현위치.
다른 운영시스템에 얹을 목적이라 **저 저장소를 import 하지 않는다** — 이 폴더만 있으면 돈다.

읽기 전용이다. 쓰기·잡 제출·삭제 엔드포인트가 하나도 없다(그래서 외부에 열어도 된다).

DB 의존
-------
PostGIS 두 테이블만 읽는다.
  terrain.road_line   도로 중심선 1,815만 건 (도로 타일)
  terrain.boundary    경계 색인 21,488건 (연구지역 선택 — 표시용 간이화 도형)
DB 가 없는 환경에 옮기려면 README 의 '오프라인 패키징' 절을 볼 것.

★도로를 GeoJSON 으로 주지 않는 이유
-----------------------------------
`road_line` 은 1,815만 건 7GB 다. GeoJSON 으로 화면 한 장을 주면 z13 에서 **48.96MB**
(그나마 4만 건에서 잘린 값)·2.13s 다. 같은 화면이 **MVT 로는 85KB · 10ms** 다.

★타일 질의는 인덱스가 사는 방향으로 써야 한다
---------------------------------------------
처음엔 `ST_Transform(r.geom, 3857) && tile_envelope` 로 짰다가 **z13 한 타일에 5.2초**가 걸렸다 —
왼쪽에 함수가 걸려 5186 GIST 인덱스를 못 쓰고 전 행을 변환한다.
**타일 봉투를 5186 으로 뒤집으면** 인덱스를 탄다. 같은 타일이 5,193ms → 33ms 가 됐다(157배).

실행
    cp .env.example .env && vi .env          # DB_URL 만 채우면 된다
    pip install -r requirements.txt
    uvicorn server.app:app --host 0.0.0.0 --port 8090
"""
from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DB_URL = os.environ.get("DB_URL")
if not DB_URL:
    raise SystemExit("DB_URL 이 없다 — .env.example 를 .env 로 복사해 채울 것")

# 풀은 작게. 이 앱은 타일 질의만 하고 오래 쥐지 않는다.
engine = create_engine(DB_URL, pool_size=4, max_overflow=6, pool_pre_ping=True)

WEB = ROOT / "web"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

app = FastAPI(title="현장 지도 (field-map)", version="1.0",
              description="연구지역·배경지도·도로(중심선)·GPS — 읽기 전용")
# 다른 시스템의 페이지가 이 서버를 직접 부를 수 있게. 운영에서는 도메인을 좁힐 것.
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["GET"], allow_headers=["*"])


# ── 벡터 타일 (MVT) ──────────────────────────────────────────────────────────
# 화이트리스트. 아무 테이블이나 타일로 뽑히면 안 된다(주입·부하).
#   minzoom  이보다 넓게 보면 빈 타일. road_line 은 z11 에서 한 타일 1.28MB 인데다
#            행 상한에 걸려 **잘린다** — 잘린 도로망은 '틀린 지도'라 아예 안 준다.
#   props    타일에 실을 속성. 적게 실어야 한다(원본 속성이 응답의 43% 였다).
MVT_LAYERS: dict = {
    "road_line": {"table": "road_line", "minzoom": 12,
                  "props": {"name": '"명칭"'}},
}
MAX_ZOOM = 22
TILE_ROW_LIMIT = 60_000        # 도심 폭주 방지. 넘으면 X-Truncated: 1 로 알린다


@app.get("/tiles/layers", tags=["타일"])
def tile_layers():
    return {"layers": [{"layer": k, "minzoom": v["minzoom"]} for k, v in MVT_LAYERS.items()],
            "max_zoom": MAX_ZOOM}


@app.get("/tiles/{layer}/{z}/{x}/{y}.pbf", tags=["타일"])
def vector_tile(layer: str, z: int, x: int, y: int):
    spec = MVT_LAYERS.get(layer)
    if spec is None:
        raise HTTPException(404, f"타일 레이어 없음: {layer} (사용 가능: {list(MVT_LAYERS)})")
    if not (0 <= z <= MAX_ZOOM):
        raise HTTPException(400, f"z 범위 밖: {z}")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise HTTPException(400, f"타일 좌표 범위 밖: {z}/{x}/{y}")
    if z < spec["minzoom"]:
        return Response(b"", media_type="application/vnd.mapbox-vector-tile",
                        headers={"Cache-Control": "public, max-age=3600", "X-Truncated": "0"})

    # src 에서는 원본 표현식을 별칭으로, 다음 CTE 에서는 **별칭만** 쓴다.
    #   ★같은 문자열을 두 번 쓰면 두 번째 CTE 가 원본 컬럼을 찾다가 터진다
    #     (road_line 의 한글 컬럼 "명칭" 으로 실제로 UndefinedColumn 이 났다).
    cols_src = "".join(f", {e} AS {a}" for a, e in spec["props"].items())
    cols_ref = "".join(f", {a}" for a in spec["props"])
    sql = text(f"""
        WITH env AS (
          SELECT ST_TileEnvelope(:z, :x, :y) AS g3857,
                 ST_Transform(ST_TileEnvelope(:z, :x, :y), 5186) AS g5186
        ), src AS (
          SELECT r.geom{cols_src}
          FROM terrain.{spec['table']} r, env
          WHERE r.geom && env.g5186          -- ★5186 끼리 비교해야 GIST 인덱스를 탄다
          LIMIT :lim
        ), mvtrow AS (
          SELECT ST_AsMVTGeom(ST_Transform(src.geom, 3857), env.g3857, 4096, 64, true) AS geom{cols_ref}
          FROM src, env
        )
        SELECT ST_AsMVT(mvtrow, :layer, 4096, 'geom'), (SELECT count(*) FROM src) FROM mvtrow""")
    with engine.connect() as con:
        row = con.execute(sql, {"z": z, "x": x, "y": y, "layer": layer,
                                "lim": TILE_ROW_LIMIT}).one()
    data = bytes(row[0]) if row[0] is not None else b""
    return Response(content=data, media_type="application/vnd.mapbox-vector-tile",
                    headers={"Cache-Control": "public, max-age=3600",
                             "X-Truncated": "1" if int(row[1] or 0) >= TILE_ROW_LIMIT else "0"})


# ── 연구지역 (경계 면) ───────────────────────────────────────────────────────
LAYER_LABELS = {"adm_sido": "시도", "adm_sigungu": "시군구", "adm_emd": "읍면동", "adm_ri": "리",
                "basin_l": "대권역", "basin_m": "중권역", "basin_s": "표준유역"}
# 전국을 한 번에 내려도 되는 건수 상한. 넘는 레이어는 bbox 를 **요구**한다.
#   ★조용히 잘라 보내지 않는다 — 잘린 줄 모르고 "전국이 다 왔다"고 믿는 쪽이 더 나쁘다.
BBOX_REQUIRED_ROWS = 3000


@app.get("/boundary/layers", tags=["연구지역"])
def boundary_layers():
    with engine.connect() as con:
        rows = dict(con.execute(text(
            "SELECT layer, count(*) FROM terrain.boundary GROUP BY layer")).all())
    out = []
    for ly, label in LAYER_LABELS.items():
        n = int(rows.get(ly, 0))
        # needs_bbox 는 **서버가** 알려 준다 — 임계값 사본을 화면에 두면 한쪽만 413 을 맞는다
        out.append({"layer": ly, "label": label, "count": n,
                    "needs_bbox": n > BBOX_REQUIRED_ROWS})
    return {"groups": [{"group": "adm", "label": "행정구역",
                        "layers": [x for x in out if x["layer"].startswith("adm_")]},
                       {"group": "water", "label": "수자원 단위지도",
                        "layers": [x for x in out if x["layer"].startswith("basin_")]}]}


def _bbox(bbox: str):
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox.split(","))
    except Exception:                                          # noqa: BLE001
        raise HTTPException(400, f"bbox 형식 오류: {bbox!r} (minx,miny,maxx,maxy)") from None
    return x1, y1, x2, y2


@app.get("/boundary/{layer}/items", tags=["연구지역"])
def boundary_items(layer: str, bbox: str | None = Query(None, description="minx,miny,maxx,maxy (4326)")):
    if layer not in LAYER_LABELS:
        raise HTTPException(404, f"경계 레이어 없음: {layer}")
    where, params = "WHERE b.layer = :layer", {"layer": layer}
    if bbox:
        x1, y1, x2, y2 = _bbox(bbox)
        params |= {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        where += (" AND b.geom && ST_Transform("
                  "ST_MakeEnvelope(:x1,:y1,:x2,:y2,4326), 5186)")
    else:
        with engine.connect() as con:
            n = con.execute(text("SELECT count(*) FROM terrain.boundary WHERE layer=:l"),
                            {"l": layer}).scalar() or 0
        if n > BBOX_REQUIRED_ROWS:
            raise HTTPException(413, f"{LAYER_LABELS[layer]}는 {n:,}건이라 전국을 한 번에 받을 수 "
                                     f"없습니다 — bbox 를 주세요. 상한 {BBOX_REQUIRED_ROWS:,}건")
    sql = text(f"""
        SELECT jsonb_build_object('type','FeatureCollection',
          'features', coalesce(jsonb_agg(f ORDER BY ord), '[]'::jsonb))::text
        FROM (
          SELECT row_number() OVER (ORDER BY b.area_km2 DESC) AS ord,
                 jsonb_build_object('type','Feature','id', b.code,
                   'geometry', ST_AsGeoJSON(ST_Transform(b.geom, 4326), 5)::jsonb,
                   'properties', jsonb_build_object(
                     'layer', b.layer, 'code', b.code, 'name', b.name,
                     'parent_name', p.name,
                     'area_km2', round(b.area_km2::numeric, 1),
                     -- 라벨 대표점: **최대 파트의 ST_PointOnSurface**.
                     -- ★중심점이 아니다 — 경기도처럼 도넛이면 중심점이 구멍(서울) 안에 떨어진다.
                     'lon', round(ST_X(lbl.pt)::numeric, 5),
                     'lat', round(ST_Y(lbl.pt)::numeric, 5))) AS f
          FROM terrain.boundary b
          LEFT JOIN terrain.boundary p ON p.layer=b.parent_layer AND p.code=b.parent_code
          CROSS JOIN LATERAL (
            SELECT ST_Transform(ST_PointOnSurface(d.geom), 4326) AS pt
            FROM (SELECT (ST_Dump(b.geom)).geom AS geom) d
            ORDER BY ST_Area(d.geom) DESC LIMIT 1) lbl
          {where}) s""")
    with engine.connect() as con:
        gj = con.execute(sql, params).scalar()
    return Response(content=gj, media_type="application/geo+json")


@app.get("/boundary/{layer}/features/{code}", tags=["연구지역"])
def boundary_feature(layer: str, code: str):
    if layer not in LAYER_LABELS:
        raise HTTPException(404, f"경계 레이어 없음: {layer}")
    with engine.connect() as con:
        row = con.execute(text("""
            SELECT b.name, p.name AS parent_name, b.area_km2,
                   ST_XMin(e.g) x1, ST_YMin(e.g) y1, ST_XMax(e.g) x2, ST_YMax(e.g) y2
            FROM terrain.boundary b
            LEFT JOIN terrain.boundary p ON p.layer=b.parent_layer AND p.code=b.parent_code
            CROSS JOIN LATERAL (SELECT ST_Transform(b.bbox, 4326) g) e
            WHERE b.layer=:l AND b.code=:c"""), {"l": layer, "c": code}).mappings().first()
    if not row:
        raise HTTPException(404, f"경계 없음: {layer}/{code}")
    return {"layer": layer, "code": code, "name": row["name"],
            "parent_name": row["parent_name"],
            "area_km2": round(row["area_km2"], 1) if row["area_km2"] is not None else None,
            "bbox_4326": [row["x1"], row["y1"], row["x2"], row["y2"]]}


@app.get("/healthz", tags=["운영"])
def healthz():
    with engine.connect() as con:
        con.execute(text("SELECT 1"))
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=str(WEB)), name="web")
