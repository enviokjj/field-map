# 지도 라벨 글리프 (0-255)

maplibre 는 한글·한자·가나를 `localIdeographFontFamily`(4.7.1 기본값 `"sans-serif"`)로
**브라우저 로컬 폰트에서** 그린다. 그래서 순수 한글 지명은 폰트 서빙이 필요 없다.

문제는 한글이 아닌 글자다. 행정구역 지명 20,500개를 훑으면 이런 문자가 섞여 있다:

    ( ) 1 2 3 4 5 6 7 8 · 坪 基 山 岐 岩 平 沙 花 華

한자 9자는 `\p{Ideo}` 라 로컬로 그려진다. 남는 건 **숫자·괄호·가운뎃점 = 전부 0-255 범위**다.
읍면동 435개(`경동1가`·`금남로1가` 등)와 리 10개, 유역 이름이 여기 걸린다.
스타일에 `glyphs` URL 이 없으면 maplibre 가 `glyphsUrl is not set` 을 던져 **그 라벨이 통째로
안 뜬다**. 그래서 0-255 한 범위만 자체 호스팅한다.

    Noto Sans Regular/0-255.pbf   223 glyphs · 76,580 B
    출처: https://demotiles.maplibre.org/font/Noto%20Sans%20Regular/0-255.pbf (2026-08-27)
    폰트: Noto Sans (SIL Open Font License 1.1)

★직접 만들지 않고 검증된 팩을 받은 이유 — 이 포맷의 `top` 은 FreeType 의 `bitmap_top` 이
**아니다**. 실측하면 숫자·`A` 가 `top=-9`, `·` 가 `top=-16` 이다(24px·buffer 3 기준).
fontnik 규약으로 짐작해 만들었으면 26px 어긋난 라벨이 나왔을 것이다.

쓰는 곳: `web/js/build.js` 의 지도 스타일 `glyphs` + 심볼 레이어 `text-font:["Noto Sans Regular"]`.
범위를 늘려야 하면(로마자 지명 등) 같은 출처에서 `256-511.pbf` 처럼 받아 이 폴더에 두면 된다.
