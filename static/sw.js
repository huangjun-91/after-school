// Service Worker - 课后服务报名系统
// 策略：网络优先（保证数据最新），失败时回退缓存（离线可用基础外壳）
const CACHE = 'afterschool-v1';
const APP_SHELL = [
  '/',
  '/login',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// 安装：预缓存基础外壳
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

// 激活：清理旧缓存
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// 请求：网络优先，失败回退缓存
self.addEventListener('fetch', (e) => {
  // 仅处理 GET，且只缓存同源请求
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;

  // 登录 POST 不缓存，GET 页面走网络优先
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        // 成功响应缓存到同源 GET（避免缓存登录后跳转混乱，只缓存 200 页面）
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match('/')))
  );
});
