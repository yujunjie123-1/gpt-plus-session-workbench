export function loopbackOrigin(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password) return '';
    return url.origin;
  } catch { return ''; }
}

export function jobForSession(jobs, session, incognito) {
  const email = session?.user?.email;
  if (typeof email !== 'string' || !session.accessToken) return null;
  const expires = Date.parse(session.expires);
  if (!Number.isFinite(expires) || expires <= Date.now()) return null;
  return jobs.find(job => job.incognito === incognito && job.expires_at * 1000 > Date.now()
    && typeof job.email === 'string' && job.email.trim().toLowerCase() === email.trim().toLowerCase()) || null;
}

export function cookieSession(cookies, storeId) {
  const usable = cookies.filter(cookie => cookie.storeId === storeId
    && cookie.domain.replace(/^\./, '') === 'chatgpt.com'
    && cookie.secure && cookie.path === '/'
    && (!cookie.partitionKey || cookie.partitionKey.topLevelSite === 'https://chatgpt.com'));
  for (const base of ['__Secure-next-auth.session-token', '__Secure-authjs.session-token', 'next-auth.session-token', 'authjs.session-token']) {
    const direct = usable.filter(cookie => cookie.name === base);
    if (new Set(direct.map(cookie => cookie.value)).size > 1) return '';
    if (direct[0]?.value) return direct[0].value;
    const chunks = usable.filter(cookie => cookie.name.startsWith(base + '.') && /^\d+$/.test(cookie.name.slice(base.length + 1)));
    const indexes = new Map();
    for (const chunk of chunks) {
      const index = Number(chunk.name.slice(base.length + 1));
      if (indexes.has(index) && indexes.get(index) !== chunk.value) return '';
      indexes.set(index, chunk.value);
    }
    const sorted = [...indexes].sort((a, b) => a[0] - b[0]);
    if (sorted.length && sorted.every(([index], position) => index === position)) return sorted.map(([, value]) => value).join('');
  }
  return '';
}
