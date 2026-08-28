#!/usr/bin/env python3
"""메모 저장·삭제·되돌리기를 **실제로 실행해** 확인한다.

★정적 검사(문자열이 있나)로는 "두 번 눌러야 지워진다"를 확인할 수 없다.
  실행만이 잡는다 — web/index.html 의 **원본 바이트**를 그대로 떼어다
  duktape 에서 돌린다(파이썬 재구현본을 만들지 않는다. 만들면 쌍둥이가 되어 갈라진다).

    python tools/memo_behavior.py
"""
import io, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEED = ["MEMO_KEY", "MEMO_MODES", "MEMO_LABEL", "memoModeIdx", "drawPts", "memoMode",
        "memoRead", "memoWrite", "memoAnchor", "memoRender", "draftRender",
        "memoClose", "memoEditId", "memoTrash", "clearArm",
        "memoClearUI", "memoClearAll", "memoUndoClear"]


def carve(src):
    """필요한 최상위 선언만 **원본 바이트 그대로** 오려낸다."""
    import esprima
    tree = esprima.parseScript(src, options={"range": True})
    out, seen = [], set()
    for node in tree.body:
        names = []
        if node.type == "FunctionDeclaration" and node.id:
            names = [node.id.name]
        elif node.type == "VariableDeclaration":
            names = [d.id.name for d in node.declarations if d.id.type == "Identifier"]
        if any(n in NEED for n in names):
            out.append(src[node.range[0]:node.range[1]])
            seen.update(names)
    missing = [n for n in NEED if n not in seen]
    if missing:
        sys.exit(f"✗ 원본에서 못 찾은 선언: {missing} — 이름이 바뀌었으면 NEED 를 고칠 것")
    return "\n".join(out)


STUB = r"""
var LOG = [], STORE = {}, TIMERS = {}, TID = 0;
var localStorage = {
  getItem: function(k){ return (k in STORE) ? STORE[k] : null; },
  setItem: function(k,v){ STORE[k] = String(v); }
};
function setTimeout(fn, ms){ TID += 1; TIMERS[TID] = fn; return TID; }
function clearTimeout(id){ delete TIMERS[id]; }
function fireTimers(){ var ks=Object.keys(TIMERS); ks.forEach(function(k){ var f=TIMERS[k]; delete TIMERS[k]; f(); }); return ks.length; }
function status(m, warn){ LOG.push((warn?"!":"") + m); }
var ELS = {};
var document = { getElementById: function(id){
  if(!ELS[id]) ELS[id] = { id:id, style:{}, textContent:"", value:"",
    classList:{ _s:{}, toggle:function(c,v){ this._s[c]=!!v; }, add:function(c){ this._s[c]=true; },
                remove:function(c){ this._s[c]=false; }, contains:function(c){ return !!this._s[c]; } } };
  return ELS[id]; } };
var map = { getSource: function(){ return null; }, getLayer: function(){ return null; },
            getCanvas: function(){ return {style:{}}; },
            dragPan: { enable:function(){}, disable:function(){} } };
var console = { warn: function(){}, log: function(){} };
function seed(n){
  var fs=[]; for(var i=0;i<n;i++) fs.push({type:"Feature",
    properties:{id:"m"+i, text:"메모"+i},
    geometry:{type:"Point", coordinates:[128.1+i*0.001, 38.0]}});
  STORE[MEMO_KEY] = JSON.stringify({type:"FeatureCollection", features:fs});
}
function count(){ return memoRead().features.length; }
"""

SCENARIO = r"""
var R = [];
function step(name, fn, want){ var got = fn(); R.push({name:name, got:got, want:want, ok: got === want}); }

seed(3);
step("씨앗 3개",                     function(){ return count(); }, 3);
step("① 한 번 눌러도 안 지워진다",    function(){ memoClearAll(); return count(); }, 3);
step("① 경고를 띄운다",              function(){ return LOG[LOG.length-1].charAt(0) === "!"; }, true);
step("① 버튼이 '한 번 더' 로 바뀐다", function(){ return document.getElementById("btnMemoClear").textContent.indexOf("한 번 더") >= 0; }, true);
step("② 두 번째에 지워진다",          function(){ memoClearAll(); return count(); }, 0);
step("③ 되돌리면 3개가 살아난다",     function(){ memoUndoClear(); return count(); }, 3);
step("③ 되돌리기는 한 번뿐",          function(){ memoUndoClear(); return count(); }, 3);

/* ★무장이 5초 뒤 저절로 풀려야 한다 — 안 풀리면 한참 뒤 무심코 누른 한 번에 전부 날아간다 */
seed(4);
step("④ 무장 → 시간이 지나면 풀린다", function(){ memoClearAll(); fireTimers(); memoClearAll(); return count(); }, 4);
step("④ 그래도 다음 한 번엔 지워진다", function(){ memoClearAll(); return count(); }, 0);

/* ★저장이 막힌 기기(비공개 모드 등)에서 '지웠다'고 말하면 안 된다 */
seed(2); memoTrash = null;
localStorage.setItem = function(){ throw new Error("QuotaExceeded"); };
step("⑤ 저장 실패면 안 지워진다",     function(){ memoClearAll(); memoClearAll(); return count(); }, 2);
step("⑤ 되돌리기를 내걸지 않는다",    function(){ return memoTrash === null; }, true);
JSON.stringify(R);
"""


def main():
    src = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf8").read()
    blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
    body = max(blocks, key=len)
    import dukpy
    res = json.loads(dukpy.evaljs(STUB + carve(body) + SCENARIO))
    bad = 0
    print("\n메모 전체삭제 — 실행 확인")
    print("─" * 62)
    for r in res:
        ok = r["ok"]; bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {r['name']:32s} {r['got']!r}" + ("" if ok else f"  (기대 {r['want']!r})"))
    print("─" * 62)
    print(f"{len(res)-bad}/{len(res)} 통과" + ("" if not bad else f" — 실패 {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
