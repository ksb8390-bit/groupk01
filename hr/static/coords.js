// 근무지 좌표 입력 도우미 (설정·근무지 수정 화면 공용)
(function () {
  const lat = document.querySelector('[name=lat]'), lon = document.querySelector('[name=lon]');
  if (!lat || !lon) return;
  // 1) "33.4996, 126.5312" 처럼 한 번에 붙여넣으면 위도·경도로 나눔 (구글 지도 오른쪽 클릭 → 좌표 복사)
  function split(el) {
    const m = (el.value || '').match(/(-?\d{1,3}\.\d+)\s*[, ]\s*(-?\d{1,3}\.\d+)/);
    if (m) { lat.value = (+m[1]).toFixed(6); lon.value = (+m[2]).toFixed(6); }
  }
  [lat, lon].forEach(el => el.addEventListener('input', () => split(el)));
  lat.placeholder = '위도 (또는 "33.xxx, 126.xxx" 붙여넣기)';
  // 2) 지도에서 좌표 찾기: 근무지명(주소)으로 구글 지도 검색 창 열기
  const name = document.querySelector('[name=name]');
  const mapBtn = document.getElementById('findmap');
  if (mapBtn) mapBtn.onclick = () => {
    const q = (name && name.value.trim()) || '제주';
    window.open('https://www.google.com/maps/search/' + encodeURIComponent(q), '_blank', 'noopener');
    alert('지도에서 해당 위치를 길게 누르거나(휴대폰) 마우스 오른쪽 클릭(PC) → 맨 위 숫자(33.xxx, 126.xxx)를 복사해 위도 칸에 붙여넣으세요.');
  };
  // 3) 현재위치: HTTPS(또는 이 컴퓨터 localhost)에서만 가능
  const here = document.getElementById('here');
  if (here) here.onclick = () => {
    if (!window.isSecureContext || !navigator.geolocation) {
      alert('휴대폰 위치는 HTTPS 주소에서만 쓸 수 있습니다.\n지금 주소(http://…)에서는 \'지도에서 좌표 찾기\'로 좌표를 붙여넣으세요.\n(클라우드 서버 https://hr.groupk.co.kr 로 옮기면 현장에서 현재위치 버튼을 쓸 수 있습니다.)');
      return;
    }
    navigator.geolocation.getCurrentPosition(p => {
      lat.value = p.coords.latitude.toFixed(6); lon.value = p.coords.longitude.toFixed(6);
    }, e => alert('위치를 가져오지 못했습니다. 휴대폰 설정에서 브라우저 위치 권한을 허용하세요. (' + e.message + ')'),
    {enableHighAccuracy: true, timeout: 10000});
  };
})();
