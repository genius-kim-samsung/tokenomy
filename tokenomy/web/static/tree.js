// 내역 트리: 날짜/폴더 그룹 행 클릭 → 자식 행 접기/펼치기.
// 이벤트 위임이라 htmx 부분 swap 후에도 재초기화 없이 동작한다.
(function () {
  function esc(s) { return (s || '').replace(/"/g, '\\"'); }

  function descendants(grp) {
    var table = grp.closest('table');
    if (!table) return [];
    var date = grp.getAttribute('data-date');
    var same = Array.prototype.slice.call(
      table.querySelectorAll('tr[data-date="' + esc(date) + '"]'));
    if (grp.classList.contains('grp-date')) {
      return same.filter(function (r) { return r !== grp; });   // 그 날짜 전체(폴더+세션)
    }
    var folder = grp.getAttribute('data-folder');               // 폴더 그룹: 그 폴더 세션만
    return same.filter(function (r) {
      return r !== grp && r.classList.contains('leaf')
             && r.getAttribute('data-folder') === folder;
    });
  }

  function setCollapsed(grp, collapsed) {
    grp.classList.toggle('collapsed', collapsed);
    descendants(grp).forEach(function (r) { r.hidden = collapsed; });
    if (!collapsed && grp.classList.contains('grp-date')) {
      // 날짜를 펼치면 자식 폴더 그룹은 펼침 상태로 리셋(전부 보이도록)
      var date = grp.getAttribute('data-date');
      grp.closest('table').querySelectorAll(
        'tr.grp-folder[data-date="' + esc(date) + '"]'
      ).forEach(function (f) { f.classList.remove('collapsed'); });
    }
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    if (e.target.closest('a')) return;                          // 링크 클릭은 통과
    var grp = e.target.closest('tr.grp');
    if (grp) {
      setCollapsed(grp, !grp.classList.contains('collapsed'));
      return;
    }
    var btn = e.target.closest('#toggle-all');
    if (btn) {
      var collapse = btn.getAttribute('data-collapsed') !== 'true';
      var scope = btn.closest('#history-body') || document;
      scope.querySelectorAll('tr.grp').forEach(function (g) { setCollapsed(g, collapse); });
      btn.setAttribute('data-collapsed', collapse ? 'true' : 'false');
      btn.textContent = collapse ? '모두 펼치기' : '모두 접기';
    }
  });
})();
