#!/usr/bin/env python3
"""도형 그리기(사각형 드래그 · 자유선 펜)를 실제로 실행해 확인한다.

★자유선은 손이 흔들리는 만큼 점이 쏟아진다. 걸러내지 않으면 한 획에 수천 점이 쌓여
  저장소를 잡아먹고 그리는 것도 느려진다 — 그 **솎아내기가 실제로 도는지**는
  코드를 읽어서는 알 수 없다. 돌려 봐야 한다.

web/index.html 의 원본 바이트를 오려내 duktape 에서 돌린다(재구현본을 만들지 않는다).

    python tools/draw_behavior.py
"""
import importlib.util
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sp = importlib.util.spec_from_file_location("mb", os.path.join(ROOT, "tools", "memo_behavior.py"))
mb = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(mb)

NEED = ["MEMO_KEY", "MEMO_MODES", "MEMO_LABEL", "memoModeIdx", "drawPts", "memoMode",
        "memoRead", "memoWrite", "memoAnchor", "memoRender", "draftRender", "memoClearUI",
        "memoOpen", "memoClose", "memoEditId", "memoAdd", "memoTrash", "clearArm",
        "memoPick", "memoFinishTwoPoint",
        "rectFrom", "rectStart", "rectPreview", "freePts", "FREE_MAX",
        "freePreview", "freePush",
        "rectPt", "rectDown", "rectMove", "rectUp"]
# ★`status` 는 일부러 뺐다 — 오려 오면 내 스텁을 덮어써서 안내문을 못 본다.

STUB = r"""
var LOG = [], STORE = {}, TIMERS = {}, TID = 0, DRAFT = null;
var localStorage = { getItem:function(k){ return (k in STORE)?STORE[k]:null; },
                     setItem:function(k,v){ STORE[k]=String(v); } };
function setTimeout(fn){ TID+=1; TIMERS[TID]=fn; return TID; }
function clearTimeout(id){ delete TIMERS[id]; }
function status(m, warn){ LOG.push((warn?"!":"") + m); }
var ELS = {};
var document = { getElementById:function(id){
  if(!ELS[id]) ELS[id] = { id:id, style:{}, textContent:"", value:"",
    classList:{ _s:{}, toggle:function(c,v){this._s[c]=!!v;}, add:function(c){this._s[c]=true;},
                remove:function(c){this._s[c]=false;}, contains:function(c){return !!this._s[c];} } };
  return ELS[id]; } };
/* ★화면 좌표 ↔ 경위도. z16 에서 경도 1픽셀 = 360/(256*2^16) 도 — 화면 기준 솎아내기가
   실제로 그 값으로 도는지 보려면 이 환산이 진짜와 같아야 한다. */
var Z = 16, DPP = 360/(256*Math.pow(2,16));
var map = {
  getZoom:function(){ return Z; },
  unproject:function(p){ return {lng: 128 + p[0]*DPP, lat: 38 - p[1]*DPP}; },
  getCanvas:function(){ return {getBoundingClientRect:function(){ return {left:0, top:0}; },
                                style:{}}; },
  getCanvasContainer:function(){ return {addEventListener:function(){}}; },
  getSource:function(id){ return {setData:function(d){ if(id==="draft") DRAFT=d; }}; },
  getLayer:function(){ return null; }, addSource:function(){}, addLayer:function(){},
  removeLayer:function(){}, removeSource:function(){},
  dragPan:{ enable:function(){}, disable:function(){} }
};
var console = { warn:function(){}, log:function(){} };
function ev(x, y){ return {clientX:x, clientY:y, preventDefault:function(){}}; }
function count(){ return memoRead().features.length; }
function last(){ var f=memoRead().features; return f[f.length-1]; }
"""

SCENARIO = r"""
var R = [];
function step(name, fn, want){
  var g; try { g = fn(); } catch(e) { g = "던짐: " + e; }
  R.push({name:name, got:g, want:want, ok:g === want});
}

/* ── 자유선 ─────────────────────────────────────────────────────────── */
memoPick("free");
step("① 자유선 모드로 들어간다",       function(){ return memoMode(); }, "free");
step("① 지도 끌기를 잠근다",           function(){ return LOG[LOG.length-1].indexOf("자유선") >= 0; }, true);

rectDown(ev(100,100));
rectMove(ev(101,100));                    // 1픽셀 — 솎아내기에 걸려야 한다
step("② ★1픽셀은 버린다",              function(){ return freePts.length; }, 1);
rectMove(ev(110,100));                    // 10픽셀 — 남는다
step("② 10픽셀은 남긴다",              function(){ return freePts.length; }, 2);
rectMove(ev(110,140));
rectUp(ev(200,140));
step("③ 자유선이 저장된다",            function(){ return count(); }, 1);
step("③ 선 도형이다",                  function(){ return last().geometry.type; }, "LineString");
step("③ 솎아낸 점만 남는다",           function(){ return last().geometry.coordinates.length; }, 4);
step("③ 그리는 중 미리보기가 있었다",   function(){ return DRAFT.features.length; }, 0);

/* ★한 번 눌렀다 뗀 것은 선이 아니다 — 도형이 생기면 안 된다 */
rectDown(ev(300,300)); rectUp(ev(300,300));
step("④ 점만 찍으면 안 그린다",        function(){ return count(); }, 1);
step("④ 이유를 알려 준다",             function(){ return LOG[LOG.length-1].charAt(0); }, "!");

/* ★상한 — 손이 떨려 수천 점이 와도 거기서 멈춘다 */
rectDown(ev(0,0));
for (var i = 1; i < FREE_MAX + 500; i++) rectMove(ev(i*10, 0));
step("⑤ 상한에서 멈춘다",              function(){ return freePts.length; }, FREE_MAX);
rectUp(ev(99999,0));
step("⑤ 상한대로 저장된다",            function(){ return last().geometry.coordinates.length; }, FREE_MAX);

/* ── 사각형은 그대로 동작해야 한다 ──────────────────────────────────── */
memoPick("polygon");
rectDown(ev(100,100)); rectMove(ev(200,200)); rectUp(ev(200,200));
step("⑥ 사각형도 여전히 그려진다",     function(){ return last().geometry.type; }, "Polygon");
step("⑥ 닫힌 고리 5점",               function(){ return last().geometry.coordinates[0].length; }, 5);

/* ★모드를 끄면 지도 끌기가 돌아와야 한다(잠긴 채로 남으면 지도가 안 움직인다) */
var freed = false;
map.dragPan.enable = function(){ freed = true; };
memoPick(null);
step("⑦ 모드를 끄면 지도 끌기 복구",   function(){ return freed; }, true);
JSON.stringify(R);
"""


def main():
    src = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf8").read()
    body = max(re.findall(r"<script>(.*?)</script>", src, re.S), key=len)
    mb.NEED = NEED
    import dukpy
    res = json.loads(dukpy.evaljs(STUB + mb.carve(body) + SCENARIO))
    bad = 0
    print("\n도형 그리기 — 실행 확인")
    print("─" * 62)
    for r in res:
        bad += 0 if r["ok"] else 1
        print(f"  {'✓' if r['ok'] else '✗'} {r['name']:32s} {r['got']!r}"
              + ("" if r["ok"] else f"  (기대 {r['want']!r})"))
    print("─" * 62)
    print(f"{len(res)-bad}/{len(res)} 통과" + ("" if not bad else f" — 실패 {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
