# 현장 지도 (field-map)

지형분석 통합 서버(`spatial_analysis`)의 **구축 화면에서 네 가지만** 떼어낸 독립 앱.

| | |
|---|---|
| ① 연구지역 | 서버에 등록된 AOI(`인제 훈련`)를 그대로 표시 · 리(법정리)는 z11+ 상시 |
| ② 배경지도 선택 | 일반(OSM) · 브이월드 · 위성(+지명) · 지형(선택) |
| ③ 도로(중심선) | `terrain.road_line` — 벡터 타일(MVT). 버튼 하나가 **100%→70%→40%→끔** 순환(기본 70%) |
| ④ GPS 현위치 | 정확도 원 + 따라가기 |
| ⑤ 메모 | **점 · 선(두 점) · 사각형(대각 두 점)** 을 그리고 글을 남긴다 — **이 기기에만** 저장(localStorage) · GeoJSON 내보내기 |

보여줄 연구지역은 굽기 옵션으로 정한다: `--aoi "인제 훈련"` (기본값).
★'연구지역 선택'(면 클릭) 도구는 **뺐다** — 이 화면의 목적은 이미 정해진 연구지역을
  현장에서 확인하는 것이라 고르는 기능이 필요 없다.

**폰·태블릿 대응**: 터치 타깃 44px · 노치 안전영역 · 주소창 높이(dvh) · 회전 잠금 ·
엄지로 누르는 큰 현위치 버튼 · 따라가기(지도를 끌면 해제).

**원 저장소를 import 하지 않는다.** 이 폴더만 있으면 돈다. 그리고 **읽기 전용**이다 —
쓰기·잡 제출·삭제 엔드포인트가 하나도 없다(그래서 외부에 열어도 된다).

---

## 두 가지 배포 방식

### A. 정적 배포 — GitHub Pages (서버 없음) ★권장

`tools/build_static.py` 가 도로 타일과 경계를 **파일로 구워** `docs/` 를 만든다.
그 폴더를 그대로 올리면 **서버도 DB도 필요 없다.**

```bash
cp .env.example .env && vi .env          # 굽는 동안만 DB 가 필요하다
pip install -r requirements.txt
python tools/build_static.py             # 기본: 인제군 z12~16 → docs/
```

굽는 범위는 **연구지역(AOI) 자체**다(`--extent aoi`, 기본). 시군구 전체를 구우면
17배를 낭비한다 — 인제군 0.65°×0.67° vs AOI 0.19°×0.13°.

실측 (인제 훈련 AOI, z12~16):

```
도로 타일 1,526개 3.5MB   (빈 타일 431개 생략)
리 29건 · AOI 1건 · 전체 1,533파일 3.7MB
```

참고 — 시군구 전체(`--extent region`)로 구우면:

```
z12    68 타일  5.2MB      시도     16건 0.54MB (전국)
z13   230 타일  5.0MB      시군구  256건 1.80MB (전국)
z14   825 타일  5.4MB      읍면동   35건 0.18MB (지역)
z15 2,636 타일  5.6MB      리      235건 0.58MB (지역)
z16 7,947 타일  6.2MB      유역 3종        1.70MB
────────────────────────────────────────────────────
합계 11,719 파일 · 32.4MB  → docs/          (빈 타일 12,951개는 안 굽는다 — 53%)
```

GitHub Pages 한도(사이트 1GB · 파일 100MB)에 여유롭게 들어간다.

```bash
git add -A && git commit -m "현장 지도" && git push
# GitHub → Settings → Pages → Source: Deploy from a branch → main / **docs**
```

★사이트는 `docs/` 다. Pages 를 `/ (root)` 로 두면 Jekyll 이 README 를 렌더해서
  **지도 대신 이 문서가 뜬다**(실제로 그렇게 됐었다). `main / docs` 로 맞출 것.

**★이 방식의 가장 큰 이점: HTTPS 가 공짜로 붙어서 GPS 가 동작한다.** (아래 §GPS)

한계도 분명하다:
- 구운 **지역·줌 범위 밖은 도로가 안 나온다**(그 밖으로 가면 빈 화면이다).
- 데이터가 **구운 시점에 고정**된다. 원본이 갱신되면 다시 구워서 다시 올려야 한다.
- 전국을 z16 까지 구우면 수십 GB라 **불가능**하다. 지역 단위로 굽는 방식이다.

### B. 서버 배포 — 이 폴더의 미니 게이트웨이

DB 에 붙어 타일을 즉석에서 만든다. 전국 어디나 되고 데이터가 항상 최신이다.

```bash
cp .env.example .env && vi .env          # DB_URL (★읽기 전용 계정 권장)
pip install -r requirements.txt
./run.sh                                 # http://127.0.0.1:8090
```

`server/app.py` 가 여는 것은 GET 5개뿐이다:
`/tiles/layers` · `/tiles/{layer}/{z}/{x}/{y}.pbf` · `/boundary/layers` ·
`/boundary/{layer}/items` · `/boundary/{layer}/features/{code}` · `/healthz`.

외부에 열려면 **HTTPS 종단이 앞에 있어야 한다**(GPS 때문에). nginx + Let's Encrypt 등.

---

## GPS — HTTPS 가 필수다

`navigator.geolocation` 은 **보안 컨텍스트에서만** 동작한다. `http://` 로 열면 브라우저가
위치를 아예 주지 않는다. 예외는 `localhost`·`127.0.0.1` 뿐이다.

- GitHub Pages → **자동 HTTPS** → 그냥 된다
- 자체 서버 → 인증서를 붙여야 한다. 안 붙이면 현위치 버튼이 "HTTPS 에서만 동작합니다"를 띄운다
  (조용히 실패하지 않게 페이지가 **먼저 막는다** — 안 그러면 "눌러도 아무 반응이 없다"가 된다)

---

## 함정 (겪은 것들)

**★V-World 인증키는 접속 도메인에 묶인다.** 새 도메인(`<계정>.github.io` 등)에서 쓰려면
[vworld.kr](https://www.vworld.kr) 에 그 도메인을 등록해야 배경 타일이 온다. 등록 전에는
브이월드·위성 배경이 통째로 비어 보인다. 키는 `web/index.html` 의 `FIELD_CFG.vworldKey`.

**★도로를 GeoJSON 으로 주면 안 된다.** `terrain.road_line` 은 **1,815만 건 · 7GB** 다.
GeoJSON 으로 화면 한 장을 주면 z13 에서 **48.96MB**(그나마 4만 건에서 잘린 값)·2.13s 다.
같은 화면이 MVT 로는 **85KB · 10ms**.

**★타일 질의는 인덱스가 사는 방향으로.** `ST_Transform(r.geom, 3857) && env` 로 쓰면
왼쪽에 함수가 걸려 5186 GIST 인덱스를 못 쓴다 — z13 한 타일에 **5.2초**가 걸렸다.
**타일 봉투를 5186 으로 뒤집으면** 33ms 다(157배).

**★z11 은 안 준다.** 한 타일이 1.28MB 인데다 행 상한 60,000 에 걸려 **잘린다**.
잘린 도로망은 '틀린 지도'라 아예 안 주는 편이 낫다(`minzoom: 12`).

**★`glyphs` 는 `style` 안에 넣어야 한다.** maplibre 는 `style.glyphs` 만 읽는다.
Map 생성자 옵션에 두면 조용히 무시되고, 숫자가 섞인 라벨 하나 때문에 **그 타일의 심볼 전체가
실패**해 한글 지명까지 통째로 사라진다.

**★한글 라벨에 0-255 글리프 팩이 필요하다.** 한글·한자는 브라우저가 로컬 폰트로 그리지만
`1지구`·`구미1리` 처럼 **숫자가 섞이면** 팩이 없을 때 그 라벨이 안 뜬다.
`web/fonts/Noto Sans Regular/0-255.pbf` (77KB) 가 그것이다.

**★읍면동·리는 전국을 한 번에 못 받는다.** 서버 모드에서는 413 으로 막고 `needs_bbox` 로
예고한다 — 조용히 잘라 보내지 않는다. 정적 모드에서는 굽는 쪽이 이미 지역으로 잘라 둔다.

---

## 구조

```
field-map/
  web/index.html          페이지 전부(HTML+CSS+JS 한 파일) · FIELD_CFG 로 설정
  web/fonts/              라벨 글리프 0-255 (77KB)
  server/app.py           미니 게이트웨이 — 읽기 전용 GET 만
  tools/build_static.py   정적 번들 굽기 → docs/  (Pages 가 서빙하는 폴더)
  tools/check.py          점검 하네스
  .env.example            DB_URL · ALLOWED_ORIGINS
```

DB 는 두 테이블만 읽는다: `terrain.road_line`(도로) · `terrain.boundary`(경계 색인).

## 점검

```bash
python tools/check.py                 # 서버 모드
python tools/check.py --static docs   # 구운 번들
```
