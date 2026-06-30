/* Portfolio Tracker – Service Worker */
const CACHE   = 'pt-v2.2.2';
const STATIC  = [
  '/',
  '/manifest.json',
  '/icons/icon-192x192.png',
  '/icons/apple-touch-icon.png',
];

// 설치: 정적 자산 캐시
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

// 활성화: 구버전 캐시 삭제
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch 전략
// - API 호출 (/api/*): Network-only (실시간 데이터 필수)
// - HTML 페이지 (/):   Network-first → Cache fallback (항상 최신 코드)
// - 정적 자산 (icons, manifest): Cache-first → Network fallback
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API: 항상 네트워크, 실패 시 오프라인 응답
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({ error: '오프라인 상태입니다. 인터넷 연결을 확인하세요.' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } })
      )
    );
    return;
  }

  // HTML 메인 페이지: Network-first (캐시된 구버전 HTML 방지)
  if (url.pathname === '/' || url.pathname.endsWith('.html')) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          if (res && res.status === 200) {
            const clone = res.clone();
            caches.open(CACHE).then(c => c.put(e.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(e.request))  // 오프라인 시 캐시 fallback
    );
    return;
  }

  // 아이콘·manifest 등 정적 자산: Cache-first → Network fallback
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (!res || res.status !== 200 || res.type !== 'basic') return res;
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      });
    })
  );
});
