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
        _check(res, "타일 템플릿의 중괄호가 살아 있다",
               # ★`html` 이 아니라 `code` 로 본다 — 왜 안 되는지 적어 둔 주석의 `%7B` 에
               #   검사가 스스로 걸린다(같은 실수를 두 번 했다).
               "/tiles/road_line/{z}/{x}/{y}.pbf" in code and "%7B" not in code,
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

        print("\n④ 타일")
        st, cat = _get(base, "/tiles/layers")
        minz = None
        if st == 200 and cat:
            r = next((l for l in cat.get("layers", []) if l["layer"] == "road_line"), None)
            minz = r and r.get("minzoom")
        _check(res, "/tiles/layers", st == 200 and minz is not None, f"road_line minzoom={minz}")
        _check(res, "minzoom 이 12 이상", (minz or 0) >= 12,
               "z11 은 한 타일 1.28MB 인데다 행 상한에 걸려 잘린다")
        tp = find_tile(base, static_dir)
        st, data = _get(base, tp, raw=True) if tp else (0, b"")
        ok = st == 200 and isinstance(data, bytes) and len(data) > 100
        lays = mvt_layers(data) if ok else []
        _check(res, "타일이 유효한 MVT", ok and lays and lays[0][0] == "road_line",
               f"{tp} · {len(data):,}B · {lays}" if ok else f"HTTP {st} {tp}")

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

        print("\n⑧ 모바일·태블릿")
        for name, tok, why in (
            ("터치 타깃 확대", "@media (pointer:coarse)", "44px — 손끝 접촉면 기준 최소값"),
            ("주소창 높이 대응", "100dvh", "100vh 는 모바일에서 출렁인다"),
            ("노치 안전영역", "env(safe-area-inset", "viewport-fit=cover 와 짝"),
            ("엄지 현위치 버튼", 'id="gpsFab"', "터치 기기에서만 뜬다"),
            ("회전 잠금", "disableRotation", "실수로 돌아가면 방향을 잃는다"),
            ("따라가기 해제 조건", 'map.on("dragstart"', "지도를 끌면 풀린다"),
        ):
            _check(res, name, tok in html, why)
        # 버튼 두 개(바·FAB)가 같은 상태를 보여야 한다 — 한쪽만 바꾸면 어긋난다
        # ★`gpsMark(` 로 3개를 세려다 틀렸다 — 정의는 `gpsMark = (on) =>` 라 안 걸린다.
        #   정의 존재 + 호출 2곳(켤 때·끌 때)으로 본다.
        _check(res, "GPS 버튼 상태 동기화",
               "const gpsMark" in html and html.count("gpsMark(") >= 2,
               "바 버튼과 엄지 버튼이 같은 상태를 보여야 한다")

        print("\n⑨ GPS 전제")
        _check(res, "HTTPS 아님을 먼저 알린다", "isSecureContext" in html,
               "안 막으면 '눌러도 아무 반응이 없다'가 된다")
        _check(res, "권한 거부·타임아웃을 구분해 알린다",
               "err.code===1" in html and "err.code===2" in html)
    finally:
        if proc:
            proc.terminate()

    bad = [n for n, ok in res if not ok]
    print(f"\n{'─'*62}\n{len(res)-len(bad)}/{len(res)} 통과"
          + (f" — \033[31m실패: {bad}\033[0m" if bad else " — \033[32m전부 통과\033[0m"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
