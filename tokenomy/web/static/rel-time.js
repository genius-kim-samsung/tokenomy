// 신선도 상대시간 tick — <time class="rel-time" data-ts="<ISO>">초기텍스트</time>를
// "방금/N분 전/N시간 전/N일 전"으로 렌더하고 주기적으로 갱신한다(표시만, 네트워크 무관).
// 서버는 절대 ISO를 data-ts로 심고 초기 텍스트(폴백)를 채운다 — JS는 그 위를 덮어쓴다.
// 임계 기준은 views._fresh_label과 동일하게 유지한다(수집/갱신 신선도 대칭, ADR 0003).
(function () {
  function rel(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var m = Math.max(0, Math.floor((Date.now() - t) / 60000));
    if (m < 1) return "방금";
    if (m < 60) return m + "분 전";
    if (m < 1440) return Math.floor(m / 60) + "시간 전";
    return Math.floor(m / 1440) + "일 전";
  }
  function tick() {
    var els = document.querySelectorAll("time.rel-time[data-ts]");
    for (var i = 0; i < els.length; i++) {
      var s = rel(els[i].getAttribute("data-ts"));
      if (s) els[i].textContent = s;
    }
  }
  tick();
  setInterval(tick, 30000);   // 30초마다 표시 갱신
  // htmx 부분교체(자동 폴링·수동 갱신)로 카드가 바뀐 뒤에도 새 data-ts를 즉시 렌더
  document.addEventListener("htmx:afterSwap", tick);
})();
