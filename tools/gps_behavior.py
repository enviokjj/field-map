#!/usr/bin/env python3
"""현위치 버튼의 **세 갈래 동작**을 실제로 실행해 확인한다.

    꺼짐          → 켠다(따라가기 시작)
    놓친 상태     → **현위치로 돌아간다**   ← 이게 안 됐다
    따라가는 중   → 끈다

★버그의 정체: `if(gpsWatch!==null){ gpsStop(); }` 가 맨 앞에 있어, 켜진 상태에서 누르면
  **무조건 꺼졌다**. 지도를 끌어 현위치를 놓친 뒤 버튼을 누르면 돌아가는 게 아니라
  GPS 가 꺼졌다 — 화면 안내문("다시 누르면 재개")과도 어긋났다.
★정적 검사로는 못 잡는다. 상태가 세 갈래라 **실행해야** 갈래가 갈린다.

web/index.html 의 원본 바이트를 오려내 duktape 에서 돌린다(재구현본을 만들지 않는다).

    python tools/gps_behavior.py
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

NEED = ["gpsWatch", "gpsFollow", "gpsLast", "gpsBtns", "gpsMark", "gpsLayers",
        "gpsStop", "gpsStart", "status"]

STUB = r"""
var LOG = [], EASE = [], HANDLERS = {}, WATCH_ID = 0, WATCH_CB = null, CLEARED = [];
var ELS = {};
var document = { getElementById: function(id){
  if(!ELS[id]) ELS[id] = { id:id, style:{}, textContent:"",
    classList:{ _s:{}, toggle:function(c,v){ this._s[c]=!!v; },
                add:function(c){ this._s[c]=true; }, remove:function(c){ this._s[c]=false; },
                contains:function(c){ return !!this._s[c]; } } };
  return ELS[id]; } };
var window = { isSecureContext: true };
var navigator = { geolocation: {
  watchPosition: function(ok, err, opt){ WATCH_CB = ok; WATCH_ID += 1; return WATCH_ID; },
  clearWatch: function(id){ CLEARED.push(id); WATCH_CB = null; } } };
var _zoom = 12;
var map = {
  getSource: function(){ return {setData:function(){}}; },
  getLayer: function(){ return null; },
  addSource: function(){}, addLayer: function(){}, removeLayer: function(){}, removeSource: function(){},
  getZoom: function(){ return _zoom; },
  easeTo: function(o){ EASE.push(o.center); },
  on: function(ev, fn){ HANDLERS[ev] = fn; }
};
var console = { warn:function(){}, log:function(){} };
var TIMERS = {}, TID = 0;
function setTimeout(fn, ms){ TID += 1; TIMERS[TID] = fn; return TID; }
function clearTimeout(id){ delete TIMERS[id]; }
function fix(lon, lat){ WATCH_CB({coords:{latitude:lat, longitude:lon, accuracy:8}}); }
function btn(){ return document.getElementById("gpsFab").classList._s; }
"""

SCENARIO = r"""
var R = [];
function step(name, fn, want){
  var g; try { g = fn(); } catch(e) { g = "던짐: " + e; }
  R.push({name:name, got:g, want:want, ok:g === want});
}

step("① 꺼진 상태에서 누르면 켜진다", function(){ gpsStart(); return gpsWatch !== null; }, true);
step("① 따라가기가 켜져 있다",        function(){ return gpsFollow; }, true);
step("① 버튼이 진한 상태",            function(){ return btn().on === true && btn().half === false; }, true);

fix(128.10, 38.00);
step("② 위치를 받으면 따라간다",      function(){ return EASE.length; }, 1);
step("② 마지막 좌표를 기억한다",      function(){ return gpsLast && gpsLast[0]; }, 128.10);

HANDLERS["dragstart"]();
step("③ 지도를 끌면 따라가기가 풀린다", function(){ return gpsFollow; }, false);
step("③ 버튼이 옅은 상태",             function(){ return btn().on === true && btn().half === true; }, true);
step("③ GPS 는 켜진 채다",             function(){ return gpsWatch !== null; }, true);

var before = gpsWatch;
gpsStart();
step("④ ★놓친 뒤 누르면 **끄지 않는다**", function(){ return gpsWatch === before; }, true);
step("④ ★현위치로 돌아간다",             function(){ return EASE.length; }, 2);
step("④ 돌아간 좌표가 마지막 위치",       function(){ return EASE[1] ? EASE[1][0] : null; }, 128.10);
step("④ 따라가기가 다시 켜진다",          function(){ return gpsFollow; }, true);

gpsStart();
step("⑤ 따라가는 중 누르면 끈다",     function(){ return gpsWatch === null; }, true);
step("⑤ 감시를 실제로 해제한다",      function(){ return CLEARED.length; }, 1);
step("⑤ 버튼이 꺼진 상태",            function(){ return btn().on === false; }, true);

/* ★위치를 한 번도 못 받은 상태에서 놓치고 눌러도 터지지 않아야 한다 */
gpsStart(); HANDLERS["dragstart"](); var n0 = EASE.length; gpsStart();
step("⑥ 좌표가 없으면 조용히 기다린다", function(){ return EASE.length === n0 && gpsFollow; }, true);
JSON.stringify(R);
"""


def main():
    src = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf8").read()
    body = max(re.findall(r"<script>(.*?)</script>", src, re.S), key=len)
    mb.NEED = NEED
    carved = mb.carve(body)
    # ★dragstart 처리기는 선언이 아니라 실행문이라 오려내기에 안 걸린다.
    #   **원본 그대로** 덧붙여 진짜 처리기를 시험한다(내가 흉내 내면 시험이 아니다).
    m = re.search(r'map\.on\("dragstart".*?\}\);', body, re.S)
    if not m:
        sys.exit("✗ dragstart 처리기를 원본에서 못 찾았다")
    import dukpy
    res = json.loads(dukpy.evaljs(STUB + carved + m.group(0) + SCENARIO))
    bad = 0
    print("\n현위치 버튼 — 실행 확인")
    print("─" * 62)
    for r in res:
        bad += 0 if r["ok"] else 1
        print(f"  {'✓' if r['ok'] else '✗'} {r['name']:34s} {r['got']!r}"
              + ("" if r["ok"] else f"  (기대 {r['want']!r})"))
    print("─" * 62)
    print(f"{len(res)-bad}/{len(res)} 통과" + ("" if not bad else f" — 실패 {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
