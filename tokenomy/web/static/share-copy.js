// 사용량 공유 문구 클립보드 복사(CONTEXT.md '사용량 공유 문구').
// 서버가 .share-src(hidden)에 문구를 렌더하고, 버튼이 이 헬퍼로 복사한다.
// navigator.clipboard → execCommand 폴백 → 둘 다 실패면 텍스트를 펼쳐 직접 선택(우아한 강등).
// 피드백은 버튼 라벨 스왑("📋 복사"→"✓ 복사됨" ~1.5s, 토스트 미신설).
(function () {
  "use strict";

  var DONE = "✓ 복사됨";

  function srcFor(btn) {
    // data-copy-target(미니뷰: 정적 바 버튼이 폴링 섹션 안을 가리킴) 우선, 없으면 같은 카드.
    var sel = btn.getAttribute("data-copy-target");
    if (sel) return document.querySelector(sel);
    var scope = btn.closest(".card") || document;
    return scope.querySelector(".share-src");
  }

  function flash(btn, msg) {
    var base = btn.getAttribute("data-share-label") || btn.textContent;
    if (!btn.getAttribute("data-share-label")) btn.setAttribute("data-share-label", base);
    // 좁은 미니 바 버튼은 data-share-done="✓"로 짧게(아이콘 폭 유지). 성공 메시지에만 적용.
    if (msg === DONE) msg = btn.getAttribute("data-share-done") || DONE;
    btn.textContent = msg;
    clearTimeout(btn._tkT);
    btn._tkT = setTimeout(function () {
      btn.textContent = btn.getAttribute("data-share-label");
    }, 1500);
  }

  function execCopy(text) {
    // user-gesture 안에서 임시 textarea로 폴백 복사. 성공 여부 boolean.
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function reveal(btn, src) {
    // 최후 강등 — 숨김 문구를 펼쳐 사용자가 직접 선택/복사하게 한다.
    if (src) {
      src.hidden = false;
      var rng = document.createRange();
      rng.selectNodeContents(src);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(rng);
    }
    flash(btn, "복사 실패 — 직접 선택");
  }

  window.tkCopyShare = function (btn) {
    var src = srcFor(btn);
    if (!src) return;
    var text = src.textContent;
    var ok = function () { flash(btn, DONE); };
    var bad = function () { if (!execCopy(text)) reveal(btn, src); else ok(); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, bad);
    } else {
      bad();
    }
  };
})();
