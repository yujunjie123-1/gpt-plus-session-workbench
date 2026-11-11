/* CDK Redemption Widget 1.0.0 — no dependencies, no supply credentials. */
(function (global) {
  'use strict';
  var PROTOCOL = 'cdk-embed-v1';
  var mounted = new WeakSet();
  var allowedStates = new Set(['loading', 'session', 'confirm', 'pending', 'completed', 'failed', 'blocked', 'error']);
  function isLocal(url) { return url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname); }
  function identifier() {
    if (!global.crypto || !global.crypto.getRandomValues) throw new Error('请使用 HTTPS 打开订单页面');
    return Array.from(global.crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, '0')).join('');
  }
  function mount(options) {
    options = options || {};
    var container = typeof options.container === 'string' ? document.querySelector(options.container) : options.container;
    if (!container || container.nodeType !== 1) throw new TypeError('container 必须指向一个页面元素');
    if (mounted.has(container)) throw new Error('此容器已有兑换组件，请先 destroy()');
    if (typeof options.getDelivery !== 'function') throw new TypeError('请提供 getDelivery 函数');
    var orderId = String(options.orderId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(orderId)) throw new TypeError('orderId 格式无效');
    var endpoint = new URL(options.endpoint || 'https://cdk.bar/embed/v1/');
    var localPage = new URL(global.location.href);
    var development = options.development === true && isLocal(endpoint) && isLocal(localPage);
    if (endpoint.protocol !== 'https:' && !development) throw new Error('兑换组件地址必须使用 HTTPS');
    if (localPage.protocol !== 'https:' && !development) throw new Error('店铺订单页必须使用 HTTPS');
    if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error('endpoint 不允许携带查询参数、凭证或片段');
    var channel = identifier();
    var iframe = document.createElement('iframe');
    iframe.title = 'ChatGPT 自助兑换';
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox');
    iframe.setAttribute('referrerpolicy', 'no-referrer');
    iframe.style.cssText = 'display:block;width:100%;height:420px;border:0;';
    endpoint.hash = new URLSearchParams({ parent: global.location.origin, channel: channel }).toString();
    iframe.src = endpoint.href;
    var destroyed = false, loadingDelivery = false, initialized = false, card = '', ready = false;
    var initialTimer;
    function notify(name, value) {
      if (destroyed || typeof options[name] !== 'function') return;
      try { options[name](value); } catch (_) { /* The merchant's callback cannot interrupt redemption. */ }
    }
    function send(type, data) {
      if (!destroyed && iframe.contentWindow) iframe.contentWindow.postMessage({ protocol: PROTOCOL, channel: channel, type: type, data: data || {} }, endpoint.origin);
    }
    async function initialize() {
      if (destroyed || !ready || initialized || loadingDelivery) return;
      loadingDelivery = true;
      try {
        var delivery = await options.getDelivery({ orderId: orderId });
        if (destroyed) return;
        if (!delivery || String(delivery.orderId) !== orderId || typeof delivery.code !== 'string') throw new Error('订单发货结果格式不正确');
        card = delivery.code.trim();
        if (!/^[A-Za-z0-9][A-Za-z0-9_-]{3,255}$/.test(card)) throw new Error('订单必须交付一张完整卡密');
        initialized = true;
        send('init', { orderId: orderId, code: card });
        card = '';
      } catch (_) {
        send('delivery-error');
        notify('onError', { code: 'DELIVERY_UNAVAILABLE', message: '暂未取得此订单的兑换资格，请确认已付款并完成发货。' });
      } finally { loadingDelivery = false; }
    }
    function onMessage(event) {
      if (destroyed || event.source !== iframe.contentWindow || event.origin !== endpoint.origin) return;
      var message = event.data;
      if (!message || message.protocol !== PROTOCOL || message.channel !== channel) return;
      if (message.type === 'ready') {
        ready = true;
        clearTimeout(initialTimer);
        if (initialized) {
          // Reloading the iframe must reload the same delivery, never buy a new card.
          initialized = false;
        }
        initialize();
      } else if (message.type === 'delivery-retry') initialize();
      else if (message.type === 'resize') {
        var height = Number(message.data && message.data.height);
        if (Number.isFinite(height) && height >= 180 && height <= 2600) iframe.style.height = Math.ceil(height) + 'px';
      } else if (message.type === 'status') {
        var data = message.data || {};
        if (!allowedStates.has(data.status)) return;
        // Deliberately whitelist fields: no CDK, tokens, Session, account email or raw API response.
        notify('onStatusChange', { orderId: orderId, status: data.status, canRetry: data.canRetry === true });
      }
    }
    global.addEventListener('message', onMessage);
    mounted.add(container);
    container.replaceChildren(iframe);
    initialTimer = setTimeout(function () {
      notify('onError', { code: 'FRAME_UNAVAILABLE', message: '兑换组件加载超时，请检查网络和店铺的 frame-src 配置。' });
    }, 20000);
    return Object.freeze({
      destroy: function () { if (destroyed) return; destroyed = true; card = ''; clearTimeout(initialTimer); global.removeEventListener('message', onMessage); iframe.remove(); mounted.delete(container); },
      refresh: function () { if (initialized) send('refresh'); else initialize(); }
    });
  }
  global.CDKRedemption = Object.freeze({ version: '1.0.0', mount: mount });
})(window);
