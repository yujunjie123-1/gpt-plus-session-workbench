/** Bind these adapters to your own authenticated order service. No supply call is made here. */
export function extractSingleCard(delivery) {
  const secret = typeof delivery === 'string' ? delivery : delivery?.data?.secret ?? delivery?.secret;
  if (typeof secret !== 'string') throw new Error('Missing delivery text');
  const text = secret.trim();
  if (/^[A-Za-z0-9][A-Za-z0-9_-]{3,255}$/.test(text)) return text;
  // The current supply shop's CardPlatformSecret::deliveredCode format.
  // Do not scrape arbitrary HTML or choose the first card from a multi-card order.
  const match = /^卡密[：:]\s*([A-Za-z0-9][A-Za-z0-9_-]{3,255})\s*\r?\n兑换地址[：:]\s*https:\/\/cdk\.bar\/?\s*$/.exec(text);
  if (match) return match[1];
  throw new Error('Expected one CDK in the documented delivery format');
}

/** Express-compatible handler; adapters are intentionally mandatory and fail closed. */
export function createRedemptionDeliveryHandler({ authenticate, loadOrder, loadDelivery, isChatGPTProduct }) {
  for (const fn of [authenticate, loadOrder, loadDelivery, isChatGPTProduct]) {
    if (typeof fn !== 'function') throw new TypeError('All four real order-system adapters are required');
  }
  return async function redemptionDelivery(req, res) {
    res.setHeader('Cache-Control', 'no-store, private');
    res.setHeader('Pragma', 'no-cache');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (req.method !== 'GET') return res.status(405).json({ error: 'METHOD_NOT_ALLOWED' });
    const id = String(req.params?.orderId || '');
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(id)) return res.status(400).json({ error: 'INVALID_ORDER_ID' });
    try {
      const user = await authenticate(req);
      if (!user?.id) return res.status(401).json({ error: 'AUTH_REQUIRED' });
      const order = await loadOrder(id);
      // Return the same 404 for absent and non-owned orders.
      if (!order || String(order.ownerId) !== String(user.id) || String(order.id) !== id) return res.status(404).json({ error: 'ORDER_NOT_FOUND' });
      if (order.paymentStatus !== 'paid' || order.fulfillmentStatus !== 'delivered' || order.quantity !== 1 || !isChatGPTProduct(order)) return res.status(409).json({ error: 'ORDER_NOT_READY' });
      const delivery = await loadDelivery(order);
      const code = extractSingleCard(delivery);
      return res.status(200).json({ orderId: id, code });
    } catch {
      // Never return/log raw delivery, session, supply credentials or exceptions.
      return res.status(503).json({ error: 'DELIVERY_UNAVAILABLE' });
    }
  };
}
