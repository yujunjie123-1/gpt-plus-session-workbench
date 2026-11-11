# 接入与使用说明

## 1. 两条链路

**供货**：代理店铺后端调用已有异次元接口，使用代理自己的 app_id/app_key 签名。供货接口的成功码通常为 `200`，返回 `data.tradeNo` 与 `data.secret`；余额扣款发生在供货端。按现有客户端实现，固定保存本次订单的 `request_no`、供货单号、发货内容，之后读取原结果。[异次元官方接口文档](https://faka.wiki/zh-cn/dev/api.html)

**兑换**：顾客订单页内的兑换组件携带该订单已交付的卡密，调用 cdk.bar 的公开兑换流程。兑换侧上游 envelope 成功码为 `0`。这两个系统的成功码不同，不要混用。

SDK 不持有供货密钥，也不包含供货下单功能。每张已交付卡密就是其持有者的兑换资格；组件不是自动扣代理余额的另一套 API。

## 2. 后端增加一个只读接口

建议路径：`GET /api/orders/:orderId/redemption-card`。

每次请求都校验：

1. 用户已认证，或通过现有游客查单认证；仅知道订单号不能读取卡密。
2. 该订单属于当前用户，且支付确认为已付款。
3. 发货已完成、数量为 1、商品处于服务端允许的 ChatGPT 商品列表。
4. 只从数据库读取原发货结果，不调用供货下单接口。

成功返回：

```json
{"orderId":"ORDER-1024","code":"SXC-EXAMPLE-ONLY"}
```

响应头：`Cache-Control: no-store, private`。无权限返回 401/404，未付款或未完成发货返回 409。不要通过缓存、Service Worker、CDN、日志或埋点复制此接口的响应。

`examples/server-adapter.mjs` 提供 Express 风格的工厂函数。四个适配器都要接到真实系统，参数缺失会拒绝初始化。其它后端语言照同样的认证顺序实现即可。没有自动完成的通用支付插件，因为每个自建站的订单模型和支付流程不同。

已登录用户校验示意：

```js
import {createRedemptionDeliveryHandler} from './server-adapter.mjs';
app.get('/api/orders/:orderId/redemption-card', createRedemptionDeliveryHandler({
  authenticate: req => existingAuth.requireUser(req),
  loadOrder: id => existingOrders.findForRedemption(id),
  loadDelivery: order => existingOrders.readSavedDelivery(order.id),
  isChatGPTProduct: order => configuredChatGPTProductIds.has(order.productId)
}));
```

以上 `existingAuth` / `existingOrders` 是你项目的实现，不是本包提供的库。`findForRedemption` 需映射到模板需要的 `id, ownerId, paymentStatus, fulfillmentStatus, quantity, productId`。

游客订单不要简单把订单号当成凭证。应沿用已存在的安全查单会话；若项目没有此能力，需要先建立不可猜的查单授权并校验本订单归属。

## 3. 解析发货内容

默认支持纯卡密，或当前供货站以下严格格式：

```text
卡密：SXC-EXAMPLE-ONLY
兑换地址：https://cdk.bar/
```

包装对象取 `data.secret` 或 `secret`。默认解析器拒绝空文本、HTML、多张卡密、其它兑换域名和内部状态标记。供货仍在等待人工发货时，不得标记成 `delivered`；应继续使用原供货订单查询发货，不能新下单。

本版一个组件对应一个业务订单的一张卡密。多数量订单请先在你自己的后端建立每件商品独立的已授权明细编号，再各挂载一个组件；不要把整段多卡密内容传入。

## 4. 挂载组件

将 `sdk/cdk-widget.js` 放到自己网站的 `/assets/cdk-widget.js`，在订单页加载：

```html
<div id="redemption"></div>
<script src="/assets/cdk-widget.js"></script>
```

```js
const widget = CDKRedemption.mount({
  container: '#redemption',
  orderId: serverRenderedOrderId,
  endpoint: 'https://cdk.bar/embed/v1/',
  async getDelivery({orderId}) {
    const response = await fetch(
      '/api/orders/' + encodeURIComponent(orderId) + '/redemption-card',
      {credentials:'same-origin', cache:'no-store'}
    );
    if (!response.ok) throw new Error('delivery unavailable');
    return response.json();
  },
  onError({message}) { showOrderMessage(message); },
  onStatusChange({status}) { updateDisplayOnly(status); }
});
```

`serverRenderedOrderId` 必须来自当前授权订单。禁止把卡密放在 `endpoint` 查询参数或 HTML 数据属性中。

React：在只针对当前订单的 `useEffect` 挂载，返回 `() => widget.destroy()`；Vue：`onMounted` 挂载，`onBeforeUnmount` 销毁。订单编号变化时先销毁旧实例。`getDelivery` 应是只读查询，组件重新载入 iframe 时可能再次调用它。

订单页应通过 HTTPS 提供。若你已有 CSP，在原策略中加入 `frame-src https://cdk.bar`；不要把其它安全策略整体删除。SDK放在你自己站点，使用现有 `script-src 'self'` 即可。若你的订单页位于多层 iframe，请确认上层的沙箱和 frame-src 也允许此用途。

## 5. 状态、恢复与重试

组件先查询原卡密：成功则展示结果，处理中则继续查询，失败并已释放则提供原卡密重试，未用则验证卡密后进入 Session。提交兑换后不会自动再次提交。

- `202` 仅表示受理，不表示已开通。
- 人工核验和多阶段支付继续显示处理中。
- 网络中断后仅查询原卡密，不新购、不自动重发兑换。
- 失败重试前会再次查询，确认可重用才重新预检。
- 结果查询采用 POST 请求体，不把卡密或兑换令牌写入查询 URL。
- 普通轮询间隔 8 秒，两分钟后 15 秒，十分钟后停止自动轮询；顾客仍可手动刷新。页面后台暂停自动轮询。
- 仅尝试保存“本订单提交待确认”标记；不保存 Session、卡密或兑换令牌。第三方存储被浏览器禁用时仍能使用组件；跨设备恢复依赖兑换站的卡密查询，最终锁定与消费由兑换后端负责。

顾客关闭页面后，Plus 升级剩余天数沿用兑换站现有后台记录逻辑；组件读取已有服务器结果。整天 × 3 元、不足一天不计，参考金额不会自动退款。旧记录缺失时显示未记录/待核验，不编造金额。

## 6. 信任边界

顾客 Session 在 cdk.bar 的 iframe 内填写，并直接通过同源请求交给兑换站。正常 SDK 不向代理页面回传 Session、兑换令牌、账号邮箱或原始响应。跨窗口通信同时校验准确 origin、source、随机通道编号；发送时使用准确 targetOrigin。[MDN postMessage 安全说明](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)

SDK 原始卡密由代理自有后端发给已授权购买者，再经消息传入组件。浏览器购买者可以查看自己收到的卡密；代理也本来就保存发货内容。因此不能把此方案当成“代理不知道卡密”或“买家无法复制卡密”。

浏览器的状态回调可以被买家伪造，**只能更新界面**。若代理后端需要核验成功或退款参考，请通过服务器使用已保存的订单卡密调用 `POST https://cdk.bar/api/v1/lookup/cdk/batch`，并核对响应中卡密对应关系。查询只需要卡密，不需要你的卡台总密钥。实际退款仍需后端订单权限、已退款金额、并发锁和财务规则。

供货 app_key 只放代理后端，代理可在自己的供货账号设置可用的对接 IP 白名单。供货余额权限和卡密兑换权限分开；卡台总密钥始终保存在供货方自己的服务器。

## 7. 嵌入策略

专用组件路径允许 HTTPS 店铺嵌入，其它兑换站页面仍保持原防嵌入策略。这个公开组件不代表获得进货、发卡或扣余额权限，兑换资格仍来自有效卡密。

供货方如需限定代理域名，可将该组件路径响应头的 `frame-ancestors https:` 改成明确的 HTTPS 店铺 origin 列表。frame-ancestors 必须放在响应头；不要只写在 HTML meta 中，也不要把白名单误当成卡密鉴权。[MDN frame-ancestors](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors)
