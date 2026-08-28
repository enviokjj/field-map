"""check.py — 현장 지도 점검 하네스.

두 배포 방식을 같은 검사로 본다.

    python tools/check.py                  # 서버 모드 (기본 http://127.0.0.1:8090)
    python tools/check.py --base http://…  # 다른 주소
    python tools/check.py --static dist    # 구운 정적 번들 (서버를 잠깐 띄워 검사한다)

  종료코드 0 = 전부 통과. 읽기 전용이라 아무것도 바꾸지 않는다.

점검 항목
  ① 페이지 문법   : index.html 의 인라인 JS 가 파싱되는가
  ② 설정 계약     : FIELD_CFG 가 있고 기본 연구지역이 잡혀 있는가
  ③ 글리프        : 0-255 팩이 있고 서빙되는가 · style **안**에 glyphs 가 있는가
                    (밖에 두면 조용히 무시돼 숫자 섞인 라벨이 있는 타일의 심볼이 전멸한다)
  ④ 타일          : /tiles/layers · 실제 타일이 유효한 MVT 이고 레이어명이 road_line 인가
  ⑤ 경계          : /boundary/layers · items 가 오고, 라벨 대표점(lon/lat)이 붙어 있는가
  ⑥ 읽기 전용     : 쓰기 메서드가 없는가(외부에 열어도 되는 근거)
  ⑦ GPS 전제      : 페이지가 **HTTPS 아님을 먼저 알려 주는가**
                    (안 그러면 "눌러도 아무 반응이 없다"가 된다)
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
_OK, _NG = "  \033[32m✓\033[0m", "  \033[31m✗\033[0m"


def _check(res, name, ok, detail=""):
    res.append((name, ok))
    print(f"{_OK if ok else _NG} {name}" + (f" — {detail}" if detail else ""))
    return ok


def _strip_js_comments(src: str) -> str:
    """// 와 /* */ 주석을 걷어낸다(문자열 리터럴 안은 건드리지 않는다).

    ★주석에 적어 둔 '하면 안 되는 것'에 검사가 스스로 걸리는 일을 막는다.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":                       # 문자열 리터럴은 통째로 통과
            q = c; out.append(c); i += 1
            while i < n:
                out.append(src[i])
                if src[i] == "\\":
                    i += 2
                    if i <= n:
                        continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                break
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c); i += 1
    return "".join(out)


def undefined_top_level(scripts):
    """**정의 없이 참조되는 이름**을 찾는다.

    ★큰 블록을 갈아 끼우다 함수를 통째로 지우는 사고가 두 번 났다
      (ADMB_LEVELS · memoOpen 외 4개). 문법은 멀쩡해서 파서로는 안 잡히고,
      화면에서는 **최상위 ReferenceError 로 스크립트가 죽어** 지도의 모든 것이 사라진다.

    ★호출(`f()`)만 세면 안 된다 — 이번 사고는 `onclick = memoSaveEdit` 처럼 **참조**였다.
      (첫 판이 호출만 세다가 재현 시험에서 못 잡았다). 그래서 식별자 참조를 전부 본다:
        · 제외 — 선언된 이름(함수·변수·매개변수·catch·for), 점 뒤 속성명, 객체 리터럴 키, 라벨
    """
    try:
        import esprima
    except ImportError:
        return []
    defined, used = set(), set()

    def add_pattern(node):
        """선언 패턴에서 이름을 거둔다(구조분해·기본값·나머지 포함)."""
        if node is None or not hasattr(node, "type"):
            return
        t = node.type
        if t == "Identifier":
            defined.add(node.name)
        elif t == "ObjectPattern":
            for pr in node.properties:
                add_pattern(getattr(pr, "value", None) or getattr(pr, "argument", None))
        elif t == "ArrayPattern":
            for el in node.elements:
                add_pattern(el)
        elif t in ("AssignmentPattern",):
            add_pattern(node.left)
        elif t in ("RestElement",):
            add_pattern(node.argument)

    def walk(node, parent=None, key=None):
        if isinstance(node, list):
            for x in node:
                walk(x, parent, key)
            return
        if not hasattr(node, "type"):
            return
        t = node.type
        if t in ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression",
                 "ClassDeclaration", "ClassExpression"):
            if getattr(node, "id", None):
                defined.add(node.id.name)
            for prm in (getattr(node, "params", None) or []):
                add_pattern(prm)
        if t == "VariableDeclarator":
            add_pattern(node.id)
        if t == "CatchClause" and getattr(node, "param", None):
            add_pattern(node.param)
        if t == "Identifier":
            # 점 뒤 속성명(a.b) · 객체 키({a:1}) · 라벨 은 참조가 아니다
            if parent is not None:
                pt = parent.type
                if pt == "MemberExpression" and key == "property" and not parent.computed:
                    return
                if pt == "Property" and key == "key" and not getattr(parent, "computed", False):
                    return
                if pt in ("LabeledStatement", "BreakStatement", "ContinueStatement"):
                    return
                if pt in ("FunctionDeclaration", "FunctionExpression", "ClassDeclaration",
                          "ArrowFunctionExpression") and key in ("id", "params"):
                    return
                if pt == "VariableDeclarator" and key == "id":
                    return
            used.add(node.name)
            return
        for k in dir(node):
            if k.startswith("_") or k in ("type", "toDict"):
                continue
            try:
                v = getattr(node, k)
            except Exception:                                     # noqa: BLE001
                continue
            if isinstance(v, list) or hasattr(v, "type"):
                walk(v, node, k)

    for src in scripts:
        walk(esprima.parseScript(src.replace("?.", ".").replace("??", "||")).body)

    GLOBALS = {
        "window","document","navigator","localStorage","sessionStorage","console","location",
        "self","globalThis","undefined","NaN","Infinity","maplibregl","fetch","setTimeout",
        "clearTimeout","setInterval","clearInterval","requestAnimationFrame","alert","confirm",
        "prompt","parseInt","parseFloat","isNaN","isFinite","encodeURIComponent","decodeURIComponent",
        "Math","JSON","Date","Promise","Object","Array","String","Number","Boolean","Error",
        "RegExp","Set","Map","WeakMap","Blob","File","URL","URLSearchParams","AbortController",
        "TextDecoder","TextEncoder","arguments","Intl",
    }
    return sorted(used - defined - GLOBALS)


def _get(base, path, raw=False):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body.decode("utf8")))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:                                        # noqa: BLE001
        return 0, str(e)


def mvt_layers(data: bytes):
    """MVT 최소 디코더 — [(레이어명, 피처수)]. 유효한 타일인지 확인하는 용도."""
    def varint(b, i):
        r = s = 0
        while True:
            x = b[i]; i += 1; r |= (x & 0x7F) << s
            if not x & 0x80:
                return r, i
            s += 7
    out, i = [], 0
    while i < len(data):
        k, i = varint(data, i)
        f, w = k >> 3, k & 7
        if w == 2:
            n, i = varint(data, i); blk = data[i:i + n]; i += n
            if f != 3:
                continue
            j, nm, feats = 0, None, 0
            while j < len(blk):
                k2, j = varint(blk, j)
                f2, w2 = k2 >> 3, k2 & 7
                if w2 == 2:
                    n2, j = varint(blk, j); v = blk[j:j + n2]; j += n2
                    if f2 == 1:
                        nm = v.decode("utf8", "replace")
                    elif f2 == 2:
                        feats += 1
                elif w2 == 0:
                    _, j = varint(blk, j)
                else:
                    j += 4 if w2 == 5 else 8
            out.append((nm, feats))
        elif w == 0:
            _, i = varint(data, i)
        else:
            i += 4 if w == 5 else 8
    return out


def find_tile(base, static_dir):
    """검사에 쓸 실제 타일 하나 고르기 — 좌표를 손으로 박으면 빈 타일을 받고 오판한다."""
    if static_dir:
        for p in sorted((static_dir / "tiles" / "road_line").rglob("*.pbf")):
            rel = p.relative_to(static_dir)
            return "/" + rel.as_posix()
        return None
    # 서버 모드 — 인제군 중심 z13 타일을 계산한다
    import math
    lat, lon, z = 38.06, 128.25, 13
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
             / math.pi) / 2 * n)
    return f"/tiles/road_line/{z}/{x}/{y}.pbf"



def glyphs_in_style(scripts):
    """new maplibregl.Map({... style:{... glyphs:… }}) 구조를 AST 로 확인한다.

    ★Map 옵션 자리(style 의 형제)에 두면 maplibre 가 조용히 무시한다 —
      `glyphManager.setURL(style.glyphs)` 로 **스타일 객체에서만** 읽기 때문이다.
      그러면 숫자 섞인 라벨 하나 때문에 그 타일의 심볼 전체가 실패한다.
    """
    try:
        import esprima
    except ImportError:
        return True, "esprima 없음 — 건너뜀"
    found = []

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not hasattr(node, "type"):
            return
        if (node.type == "NewExpression"
                and getattr(getattr(node.callee, "property", None), "name", "") == "Map"
                and node.arguments and node.arguments[0].type == "ObjectExpression"):
            opts = node.arguments[0]
            names = [getattr(p.key, "name", getattr(p.key, "value", None)) for p in opts.properties]
            style = next((p for p, n in zip(opts.properties, names) if n == "style"), None)
            inside = False
            if style is not None and style.value.type == "ObjectExpression":
                inside = any(getattr(q.key, "name", getattr(q.key, "value", None)) == "glyphs"
                             for q in style.value.properties)
            found.append((inside, "glyphs" in names))
        for k in dir(node):
            if k.startswith("_") or k in ("type", "toDict"):
                continue
            try:
                v = getattr(node, k)
            except Exception:                                     # noqa: BLE001
                continue
            if isinstance(v, list) or hasattr(v, "type"):
                walk(v)

    for src in scripts:
        walk(esprima.parseScript(src.replace("?.", ".").replace("??", "||")).body)
    if not found:
        return False, "maplibregl.Map 생성부를 못 찾음"
    if any(stray for _in, stray in found):
        return False, "glyphs 가 Map 옵션 자리에 있다 — maplibre 가 무시한다"
    return all(i for i, _ in found), ("style 안" if all(i for i, _ in found) else "style 안에 없음")

def main(argv=None):
    ap = argparse.ArgumentParser(description="현장 지도 점검")
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--static", help="구운 번들 폴더 (예: dist)")
    a = ap.parse_args(argv)
    res = []
    static_dir = (ROOT / a.static).resolve() if a.static else None
    proc = None
    base = a.base

    if static_dir:
        if not static_dir.is_dir():
            raise SystemExit(f"번들 폴더가 없다: {static_dir}")
        proc = subprocess.Popen([sys.executable, "-m", "http.server", "8098"],
                                cwd=static_dir, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:8098"
        time.sleep(1.5)

    try:
        html_path = (static_dir / "index.html") if static_dir else (ROOT / "web" / "index.html")
        html = html_path.read_text(encoding="utf8")

        print("① 페이지 문법")
        js = re.findall(r"<script>(.*?)</script>", html, re.S)
        try:
            import esprima
            ok = True
            for s in js:
                try:
                    esprima.parseScript(s.replace("?.", ".").replace("??", "||"))
                except Exception as e:                            # noqa: BLE001
                    ok = False
                    print("    ", e)
            _check(res, "인라인 JS 파싱", ok, f"{len(js)}개 · {sum(len(x) for x in js):,}자")
            # ★정의 없이 부르는 이름이 있으면 최상위 ReferenceError 로 스크립트가 죽는다 —
            #   그러면 지도의 모든 것(연구지역·도로·메모)이 한꺼번에 사라진다. 두 번 겪었다.
            miss = undefined_top_level(js)
            _check(res, "정의 없는 함수를 부르지 않는다", not miss,
                   f"없는 이름: {miss}" if miss else "블록 교체로 함수가 지워지면 여기서 걸린다")
        except ImportError:
            _check(res, "인라인 JS 파싱", True, "esprima 없음 — 건너뜀")

        print("\n② 설정 계약")
        _check(res, "FIELD_CFG 존재", "const FIELD_CFG" in html)
        m = re.search(r'region:\s*\{\s*layer:"(\w+)",\s*code:"(\d+)",\s*name:"([^"]+)"', html)
        _check(res, "기본 연구지역", bool(m), f"{m.group(3)} ({m.group(1)}:{m.group(2)})" if m else "없음")

        print("\n②b 자료 주소")
        # ★타일·글리프는 **워커**에서 요청된다. 워커의 기준 URL 은 문서와 다를 수 있어
        #   상대경로(`./tiles/…`)면 조용히 404 가 나고 "도로가 안 보인다"로만 나타난다.
        #   루트 절대경로(`/tiles/…`)도 안 된다 — 프로젝트 Pages 는 하위경로에 산다.
        # ★주석을 걷어내고 본다 — 아래 검사는 "new URL 을 **안** 쓴다"인데,
        #   왜 쓰면 안 되는지 적어 둔 주석에 스스로 걸리면 검사가 뒤집힌다.
        code = _strip_js_comments("\n".join(js))
        _check(res, "페이지 디렉터리 기준 절대화", "document.baseURI" in code and "PAGE_DIR" in code)
        # ★이게 핵심이다: URL 생성자에 넣으면 `{z}` → `%7Bz%7D` 로 인코딩돼
        #   maplibre 가 치환을 못 하고 **도로가 통째로 사라진다**(실제로 그렇게 됐다).
        _check(res, "타일 템플릿을 URL 생성자에 넣지 않는다", "new URL(" not in code,
               "{z} 가 %7Bz%7D 로 인코딩된다")
        _check(res, "글리프 주소의 중괄호가 살아 있다",
               # ★`html` 이 아니라 `code` 로 본다 — 왜 안 되는지 적어 둔 주석의 `%7B` 에
               #   검사가 스스로 걸린다(같은 실수를 두 번 했다).
               # ★도로가 파일 하나가 되면서 `{z}/{x}/{y}` 는 사라졌지만, **글리프 주소에는
               #   여전히 중괄호가 있다** — 인코딩되면 한글 라벨이 통째로 안 뜬다.
               "/fonts/{fontstack}/{range}.pbf" in code and "%7B" not in code,
               "치환되지 않으면 없는 주소를 친다")
        from urllib.parse import urljoin
        for base_url, want in (("https://x.github.io/field-map/", "https://x.github.io/field-map/tiles/layers"),
                               ("https://x.example.com/", "https://x.example.com/tiles/layers")):
            got = urljoin(base_url, "tiles/layers")
            _check(res, f"경로 해석 {base_url}", got == want, got)
        _check(res, "타일 실패를 화면에 알린다", 'map.on("error"' in html,
               "조용히 404 나면 원인을 못 찾는다")

        print("\n③ 글리프")
        # ★정규식으로 style 블록을 잘라 보려다 실패했다(중첩 괄호를 못 센다).
        #   **AST 로** 본다 — new maplibregl.Map({…}) 의 style 객체 안에 glyphs 가 있는가,
        #   그리고 style 의 **형제**(Map 옵션 자리)에 있지는 않은가.
        ok_g, why = glyphs_in_style(js)
        _check(res, "glyphs 가 style 안에", ok_g, why)
        st, body = _get(base, "/fonts/Noto%20Sans%20Regular/0-255.pbf", raw=True)
        _check(res, "글리프 팩 서빙", st == 200 and len(body) > 50_000,
               f"{len(body):,}B" if st == 200 else f"HTTP {st}")

        print("\n④ 도로 (파일 하나)")
        # ★타일 피라미드에서 **파일 하나**로 바꿨다. 연구지역 안 도로가 11,442개 선뿐이라
        #   쪼갤 이유가 없었다: 타일 11,631파일 5.2MB·z12~18 밖은 빈 화면
        #   → 한 파일 4.64MB(gzip 0.79MB)·줌 제한 없음. 그래서 minzoom 검사도 없앴다.
        st, gj = _get(base, "/roads.geojson")
        rf = (gj or {}).get("features", []) if st == 200 else []
        _check(res, "/roads.geojson", st == 200 and len(rf) > 1000,
               f"{len(rf):,}개 선" if st == 200 else f"HTTP {st}")
        kinds = sorted({f["geometry"]["type"] for f in rf}) if rf else []
        _check(res, "전부 선 도형", bool(kinds) and set(kinds) <= {"LineString", "MultiLineString"},
               f"{kinds} — 점이 섞이면 ST_Intersection 결과를 안 거른 것이다")
        _check(res, "도로에 줌 하한이 없다",
               "ROAD_MINZ" not in code and 'source:"road", "source-layer"' not in code,
               "minzoom 을 걸면 그 아래에서 도로가 사라진다 — 한 파일이라 걸 이유가 없다")
        _check(res, "도로 소스가 geojson", 'type:"geojson", data:api("/roads.geojson")' in code,
               "벡터 타일 소스가 남아 있으면 없는 타일을 친다")

        print("\n⑤ 연구지역(AOI) · 리")
        st, gj = _get(base, "/aoi/items")
        feats = (gj or {}).get("features", []) if st == 200 else []
        _check(res, "AOI 응답", st == 200 and len(feats) >= 1,
               ", ".join(f"{f['properties']['name']}({f['properties'].get('km2')}km²)"
                         for f in feats) if feats else f"HTTP {st}")
        # ★bbox 를 실어 보낸다 — 서버 모드에서 리는 전국 15,161건이라 bbox 없이는 413 이다
        #   (정적 모드에선 쿼리가 무시되고 미리 잘라 둔 파일이 온다). 페이지도 같은 방식이다.
        rib = ""
        if feats:
            xs = [c[0] for f in feats for r in f["geometry"]["coordinates"] for c in r]
            ys = [c[1] for f in feats for r in f["geometry"]["coordinates"] for c in r]
            rib = f"?bbox={min(xs):.5f},{min(ys):.5f},{max(xs):.5f},{max(ys):.5f}"
        st, gj = _get(base, f"/boundary/adm_ri/items{rib}")
        ri = (gj or {}).get("features", []) if st == 200 else []
        # ★건수 기준은 모드마다 다르다 — 서버 모드는 AOI 범위만(인제 22건),
        #   정적 모드는 인제군 전체를 구워 둔다(235건). 둘 다 만족하는 하한만 본다.
        _check(res, "리 응답", st == 200 and len(ri) >= 10, f"{len(ri)}건")
        bad = [f["properties"]["code"] for f in ri
               if not isinstance(f["properties"].get("lon"), (int, float))]
        _check(res, "리 라벨 대표점 전건", not bad,
               "폴리곤에 symbol 을 얹으면 파트마다 지명이 반복된다" if not bad else f"누락 {len(bad)}")
        # ★`riLoad()` 문자열을 찾으면 인자를 붙이는 순간 깨진다(실제로 깨졌다) — 정의로 본다
        _check(res, "리는 토글 없이 상시",
               "function riLoad" in html and "RI_MINZ" in html and "await riLoad(" in html)
        _check(res, "연구지역 선택 도구는 제거됨",
               "regionBar" not in html and "rgShow" not in html,
               "이 화면은 정해진 연구지역을 확인하는 용도다")

        print("\n⑥ 읽기 전용")
        src = (ROOT / "server" / "app.py").read_text(encoding="utf8")
        writes = re.findall(r"@app\.(post|put|patch|delete)", src)
        _check(res, "쓰기 엔드포인트 없음", not writes, f"발견: {writes}" if writes else
               "GET 만 — 외부에 열어도 되는 근거")

        print("\n⑦ 도로 가시성")
        import re as _re
        col = _re.search(r'const ROAD_COLOR\s*=\s*"([^"]+)"', html)
        _check(res, "도로 색이 상수 한 곳에", bool(col), col.group(1) if col else "없음")
        # 배경 4종이 쓰는 색(노랑·흰색·초록·베이지)과 겹치면 안 보인다
        _check(res, "배경과 겹치지 않는 색", bool(col) and col.group(1).upper() not in
               ("#FFFFFF", "#FFFF00", "#FFD400", "#C2412B"), "브이월드 도로는 노랑/주황이다")
        w = _re.search(r'"line-color":ROAD_COLOR[^}]*?"line-width":\["interpolate",\["linear"\],\["zoom"\],12,([\d.]+)', html, _re.S)
        _check(res, "z12 선 굵기 ≥1.2px", bool(w) and float(w.group(1)) >= 1.2,
               f"{w.group(1)}px" if w else "못 읽음")

        print("\n⑦b 굽는 범위 · 메모")
        # ★구운 타일이 실제로 **연구지역 범위**에 있는지 본다 — 시군구 전체를 구우면
        #   17배를 낭비하고, 엉뚱한 곳을 구우면 현장에서 도로가 안 나온다.
        if static_dir and feats and rf:
            # ★구운 도로가 실제로 **연구지역 안**인지 본다. 엉뚱한 곳을 구우면
            #   현장에서 도로가 안 나오고, 넓게 구우면 파일만 커진다.
            xs = [c[0] for f in feats for r in f["geometry"]["coordinates"] for c in r]
            ys = [c[1] for f in feats for r in f["geometry"]["coordinates"] for c in r]
            def flat(g):
                cs = g["coordinates"]
                return cs if g["type"] == "LineString" else [c for part in cs for c in part]
            rx = [c[0] for f in rf for c in flat(f["geometry"])]
            ry = [c[1] for f in rf for c in flat(f["geometry"])]
            eps = 1e-4                                   # ≈11m — 자른 경계의 반올림 여유
            ok = (min(rx) >= min(xs)-eps and max(rx) <= max(xs)+eps
                  and min(ry) >= min(ys)-eps and max(ry) <= max(ys)+eps)
            _check(res, "도로가 연구지역 범위 안", ok,
                   f"도로 {min(rx):.4f}~{max(rx):.4f} vs AOI {min(xs):.4f}~{max(xs):.4f}")
            gz = len(gzip.compress(json.dumps(gj, ensure_ascii=False).encode(), 6))
            _check(res, "전송량이 감당할 만하다", gz < 4e6,
                   f"gzip {gz/1e6:.2f}MB — 폰에서 한 번 받는 양이다")
            n_tiles = 0
            mb = sum(f.stat().st_size for f in static_dir.rglob("*") if f.is_file()) / 1e6
            _check(res, "번들이 한도 안", mb < 800, f"타일 {n_tiles:,}개 · {mb:.1f}MB")

        for name, tok, why in (
            ("메모 기능", "MEMO_KEY", "지점을 눌러 글자를 남긴다"),
            ("메모 저장소", "localStorage", "서버가 없어 이 기기에만 남는다"),
            ("메모 내보내기", "memoExport", "GeoJSON 으로 빼내 다른 지도에서 연다"),
            ("아이폰 내보내기 경로", "navigator.share",
             "iOS(크롬 포함, 내부는 WebKit)는 a[download] 로 파일을 못 받는다 — 공유 시트가 정상 경로"),
            ("기존 메모 수정·삭제", "queryRenderedFeatures", "찍힌 도형을 눌러 고친다"),
            ("도형 그리기 3종", 'MEMO_MODES', "점·선·면"),
            ("그리는 중 미리보기", "draftRender", "첫 점이 바로 보인다"),
            ("선·사각형은 두 점으로", "memoFinishTwoPoint",
             "선=시작·끝 · 면=대각 모서리. 점을 여러 개 찍고 확정하는 방식은 '점만 찍힌다'로 보였다"),
            ("사각형 생성", "rectFrom", "대각 두 점 → 닫힌 사각형 링"),
            ("라벨은 대표점 하나에", "memoAnchor",
             "도형에 직접 얹으면 타일마다 반복된다(연구지역 이름에서 겪었다)"),
        ):
            _check(res, name, tok in html, why)
        import re as _re2
        modes = _re2.search(r"const MEMO_MODES = \[([^\]]*)\]", html)
        _check(res, "모드 순환에 끔이 있다", bool(modes) and "null" in modes.group(1),
               "끄지 못하면 지도를 못 움직인다")
        # ★prompt/confirm 은 모바일 브라우저에 따라 막히거나(무반응) 화면을 가려서
        #   "점은 찍히는데 메모를 못 남긴다" 가 된다. 화면 안 입력 카드로 받아야 한다.
        #   (검사는 주석을 걷어낸 코드로 — 왜 쓰면 안 되는지 적은 주석에 걸리지 않게)
        _check(res, "브라우저 대화상자를 쓰지 않는다",
               "window.prompt" not in code and "confirm(" not in code,
               "폰에서 막히면 메모를 못 남긴다")
        _check(res, "메모 입력 카드", 'id="memoEdit"' in html and "memoSaveEdit" in html)
        _check(res, "점은 누르는 즉시 찍힌다", "memoAdd(" in code,
               "글은 그다음에 적는다 — 안 적어도 점은 남는다")

        print("\n⑦c 도로 투명도 · 켜기끄기")
        # ★버튼은 **켜고 끄기만** 한다(요청). 종전엔 100%→70%→40%→끔 을 돌아
        #   현장에서 지금 몇 번째인지 세게 만들었다. 투명도는 30% 고정.
        m_op = _re2.search(r"const ROAD_OPACITY = ([0-9.]+)", html)
        _check(res, "투명도 30% 고정", bool(m_op) and abs(float(m_op.group(1)) - 0.7) < 1e-9,
               f"ROAD_OPACITY={m_op.group(1) if m_op else '없음'} (0.7 = 30% 투명)")
        _check(res, "단계 순환을 없앴다", "ROAD_STEPS" not in html,
               "단계가 남아 있으면 버튼이 또 여러 상태를 돈다")
        _check(res, "켠 채로 시작", "roadStep = 1" in html, "100% 는 영상을 가린다")
        _check(res, "투명도를 실제로 반영", 'setPaintProperty("road-line","line-opacity"' in html)

        print("\n⑧ 모바일·태블릿")
        for name, tok, why in (
            ("터치 타깃 확대", "@media (pointer:coarse)", "44px — 손끝 접촉면 기준 최소값"),
            ("주소창 높이 대응", "100dvh", "100vh 는 모바일에서 출렁인다"),
            ("노치 안전영역", "env(safe-area-inset", "viewport-fit=cover 와 짝"),
            ("엄지 현위치 버튼", 'id="gpsFab"', "왼쪽 아래 · 현위치는 이것 하나뿐"),
            ("큰 버튼 조건에 화면폭도", "(max-width:900px)",
             "pointer:coarse 만 보면 판정 안 되는 기기에서 안 뜬다"),
            ("사각형은 드래그", "rectBind", "두 번 누르는 방식은 '그리는 모양'이 아니다"),
            ("메모 전체 삭제", "btnMemoClear", "두 번 눌러야 지워진다 · 되돌리기 있음"),
            ("회전 잠금", "disableRotation", "실수로 돌아가면 방향을 잃는다"),
            ("따라가기 해제 조건", 'map.on("dragstart"', "지도를 끌면 풀린다"),
        ):
            _check(res, name, tok in html, why)
        # ★현위치 버튼은 **하나**여야 하고 **항상** 떠 있어야 한다.
        #   바+FAB 둘로 두고 화면 조건으로 갈랐더니 기기마다 결과가 달라졌다 —
        #   처음엔 pointer:coarse 미판정으로 **둘 다** 사라졌고, 그걸 고친 뒤엔
        #   바 버튼만 남아 "왼쪽 아래로 안 갔다"가 됐다. 조건을 없앤 것을 굳힌다.
        _check(res, "현위치는 왼쪽 아래 하나뿐", 'id="btnGps"' not in html,
               "상단 바에도 현위치 버튼이 있으면 기기에 따라 그쪽만 보인다")
        _check(res, "현위치 버튼이 조건 없이 뜬다", "#gpsFab{display:flex" in html,
               "display:none 으로 시작하면 조건이 안 맞는 기기에서 사라진다")
        # ★`gpsMark(` 로 3개를 세려다 틀렸다 — 정의는 `gpsMark = (on) =>` 라 안 걸린다.
        _check(res, "GPS 상태 표시 배선",
               "const gpsMark" in html and html.count("gpsMark(") >= 2,
               "켤 때·끌 때 버튼 색이 바뀌어야 한다")
        # ★배경지도는 브이월드·위성 둘뿐이다(요청). 버튼이 늘면 현장에서 헷갈린다.
        _check(res, "배경지도는 둘뿐", html.count('data-bm="') == 2,
               "브이월드·위성만 있어야 한다")
        _check(res, "지운 배경지도의 버튼이 안 남았다",
               'data-bm="topo"' not in html and 'data-bm="osm"' not in html,
               "버튼만 남으면 눌렀을 때 아무 일도 안 일어난다")

        print("\n⑨ GPS 전제")
        _check(res, "HTTPS 아님을 먼저 알린다", "isSecureContext" in html,
               "안 막으면 '눌러도 아무 반응이 없다'가 된다")
        _check(res, "권한 거부·타임아웃을 구분해 알린다",
               "err.code===1" in html and "err.code===2" in html)

        print("\n⑩ 오프라인 (인터넷 없이 쓰기)")
        # ★첫 번째 벽은 배경지도가 아니라 **지도 라이브러리**였다 — CDN 에서 받아오니
        #   인터넷이 없으면 페이지가 아예 안 떴다. 번들에 넣은 것을 굳힌다.
        _check(res, "지도 라이브러리를 번들에서 읽는다",
               'src="vendor/maplibre-gl.js"' in html and "unpkg.com" not in html,
               "CDN 이면 인터넷 없이 페이지가 아예 안 뜬다")
        st, body = _get(base, "/vendor/maplibre-gl.js", raw=True)
        _check(res, "라이브러리 서빙", st == 200 and len(body) > 500_000,
               f"{len(body):,}B" if st == 200 else f"HTTP {st}")
        _check(res, "서비스 워커를 등록한다", 'register(PAGE_DIR + "/sw.js")' in code,
               "없으면 캐시가 10분(max-age=600)짜리라 새로고침하면 죽는다")
        st, sw = _get(base, "/sw.js", raw=True)
        swt = sw.decode("utf8", "replace") if st == 200 else ""
        _check(res, "서비스 워커 서빙", st == 200 and "fieldmap-" in swt,
               f"{len(sw):,}B" if st == 200 else f"HTTP {st}")
        # ★판 번호를 안 박으면 브라우저가 옛 파일을 계속 내준다(캐시 이름이 그대로다).
        _check(res, "서비스 워커 판 번호가 박혔다", "__VERSION__" not in swt,
               "굽는 쪽(build_static.stamp_sw)이 자동으로 박는다 — 사람이 하면 잊는다")
        _check(res, "우리 파일은 캐시 먼저", "caches.open(CACHE)" in swt and "c.match(req)" in swt)
        _check(res, "배경지도 원본은 네트워크 먼저", 'url.hostname === "api.vworld.kr"' in swt,
               "온라인에서는 최신을 쓰고, 끊기면 캐시로 떨어진다")
        _check(res, "오프라인 준비 버튼", 'id="btnOffline"' in html and "BAKE" in code,
               "79MB 를 첫 방문에 조용히 끌어가지 않는다 — 누를 때 받는다")

        if static_dir:
            man = static_dir / "basemap" / "manifest.json"
            mj = json.loads(man.read_text(encoding="utf8")) if man.is_file() else {}
            n_bm = len(mj.get("tiles", []))
            _check(res, "배경지도가 번들에 있다", n_bm > 3000,
                   f"{n_bm:,}장 · {mj.get('bytes',0)/1e6:.0f}MB · z{mj.get('zoom')}")
            real = sum(1 for _ in (static_dir / "basemap").rglob("*")
                       if _.is_file() and _.suffix in (".png", ".jpeg"))
            _check(res, "목록과 실제 파일이 맞는다", real == n_bm - (1 if n_bm else 0) or real == n_bm,
                   f"목록 {n_bm:,} vs 실제 {real:,}")
            _check(res, "배경지도를 번들에서 읽는다",
                   'bmLocal("Base","png")' in code and "LOCAL_BM_MAXZ" in code,
                   "원격만 보면 인터넷이 끊길 때 배경이 통째로 회색이 된다")
            _check(res, "z16 위는 원본을 겹친다", 'id:"basemap-hi"' in code,
                   "온라인에선 선명하게, 오프라인에선 그 겹침만 실패하고 배경은 남는다")

        print("\n⑪ 폰·태블릿 크기 조정")
        # ★종전엔 12px/14px 두 단계뿐이라 폰에선 작고 큰 태블릿에선 화면에 비해 더 작았다.
        for name, tok, why in (("글자 크기가 연속적", "--ui:   clamp(", "폰 14px ~ 태블릿 16px"),
                               ("손가락 목표 높이", "--tap:  clamp(", "40 ~ 48px"),
                               ("현위치 버튼도 비례", "--fab:  clamp(", "54 ~ 68px"),
                               ("iOS 자동확대 차단", "text-size-adjust:100%", "돌릴 때 글자가 들쭉날쭉해진다")):
            _check(res, name, tok in html, why)
        # ★상단 바는 좁은 화면에서 **한 줄로 옆으로 민다**(요청). 줄바꿈은 지도를 덮는다.
        # ★검사가 **제 주석에 스스로 걸렸다**(세 번째 — glyphs·%7B 에 이어). CSS 주석을 걷고 본다.
        css = _re2.sub(r"/\*.*?\*/", " ", html, flags=_re2.S).replace(" ", "")
        _check(res, "상단 바는 옆으로 민다",
               "flex-wrap:nowrap;overflow-x:auto" in css,
               "줄바꿈하면 바가 두 줄이 되어 지도를 덮는다")
        _check(res, "더 남았다는 표시가 있다",
               "#ctl.more{" in css and "ctlOverflow" in code,
               "표시가 없으면 오른쪽 버튼이 있는지조차 모른다 — 끝까지 밀면 사라진다")
    finally:
        if proc:
            proc.terminate()

    bad = [n for n, ok in res if not ok]
    print(f"\n{'─'*62}\n{len(res)-len(bad)}/{len(res)} 통과"
          + (f" — \033[31m실패: {bad}\033[0m" if bad else " — \033[32m전부 통과\033[0m"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
