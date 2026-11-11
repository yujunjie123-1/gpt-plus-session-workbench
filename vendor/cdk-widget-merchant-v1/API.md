# SDK 与接口约定 v1

## SDK

全局对象 `CDKRedemption`，版本 `1.0.0`，无第三方依赖。

```js
const instance = CDKRedemption.mount(options);
instance.refresh();
instance.destroy();
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `container` | 是 | CSS 选择器或 DOM 元素，一处只允许一个实例 |
| `orderId` | 是 | 1～128 位字母、数字、点、下划线、冒号或连字符，不使用邮箱等个人资料 |
| `getDelivery({orderId})` | 是 | 异步只读函数，返回 `{orderId, code}`；订单号必须完全一致 |
| `endpoint` | 否 | 默认 `https://cdk.bar/embed/v1/`；不允许查询参数或片段 |
| `onStatusChange(event)` | 否 | `{orderId, status, canRetry}`，仅展示用途 |
| `onError(event)` | 否 | `{code, message}`，获取发货失败或 iframe 加载失败提示 |
| `development` | 否 | 仅双方都在 HTTP loopback 时允许；生产不设置 |

`refresh()` 刷新已有结果或重新读取资格，不提交兑换，不下供货订单。`destroy()` 移除 iframe 和事件监听，离开页面时调用。加载超时会通过 `FRAME_UNAVAILABLE` 回调；取得订单卡密失败为 `DELIVERY_UNAVAILABLE`。错误回调不包含原异常或发货文本。

## 状态回调

| status | 含义 |
| --- | --- |
| `loading` | 读取和验证已有订单资格 |
| `session` | 等待填写 Session |
| `confirm` | 预检完成，等待顾客确认账号 |
| `pending` | 已提交/处理中/待核验，只查询 |
| `completed` | 兑换站卡密查询确认已使用成功 |
| `failed` | 本次失败，只有 `canRetry: true` 才能重试 |
| `blocked` | 卡密禁用、过期等状态 |
| `error` | 加载或状态暂未确认 |

回调不包含 Session、邮箱、卡密、兑换令牌、退款金额或完整订单。任何浏览器回调都不是后端结算凭据。

## 组件调用的专用接口

这些路径由供货方部署到兑换站，仅允许 POST、JSON、同源 Origin，浏览器不携带后台 cookies。代理正常使用 SDK 即可，不必自行拼装这些请求。生产设有独立按来源 IP 的限流，429 后应等待。

| 路径 | 请求体 | 用途 |
| --- | --- | --- |
| `/embed/v1/api/lookup` | `{codes:["单张卡密"]}` | 状态及已记录 Plus 剩余查询 |
| `/embed/v1/api/preview` | `{code:"单张卡密"}` | 验证兑换资格，获得 redemption_token |
| `/embed/v1/api/preflight` | `{code, redemption_token, credential:{mode:"session", session:"完整JSON字符串"}}` | 账号预检，获得 preflight_token |
| `/embed/v1/api/redeem` | `{redemption_token, preflight_token, client_request_id}` | 顾客确认后提交一次兑换 |

专用接口复用兑换站既有后端业务，不开放后台管理或直接发卡接口。`client_request_id` 根据本次预检票生成稳定摘要；正常流程只提交一次。资格或查询卡密不能来自任意浏览器自报的订单，必须由代理后端先验证订单归属后交付。

## 代理后端独立核验（可选）

向主兑换站既有接口发送：

```http
POST /api/v1/lookup/cdk/batch HTTP/1.1
Host: cdk.bar
Content-Type: application/json

{"codes":["该订单数据库保存的卡密"]}
```

正常响应的 `results` 中按 `cdk_code` 精确关联。成功需要 `status: "used"` 与 `used: true`；处理中为 `processing`；失败为 `failed`，是否允许再次使用另看 `can_resubmit`。不得把邮箱已有绑定当成成功。

升级退款参考位于成功记录的 `plus_refund`：只有 `state: "recorded"` 且 `remaining_full_days`、`refund_amount_fen` 为有效整数时使用。金额单位为分，`3000` 表示 30 元；缺失、`not_recorded` 或 `missing` 不能按 0 元或自行推算替代。

查询只核验状态，不自动操作支付。请对店铺自己的核验端点限流、鉴权并禁止缓存；不得让顾客传任意卡密后读取其它人的记录。
