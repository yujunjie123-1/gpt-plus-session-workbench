# Partner API 与充值归属设计

## 目标

将工作台充值服务切换到 `cdk-partner-kit-v1` 文档定义的 Partner API，解决并发登录页 `Operation timed out` 后任务悬停、充值结果不能自动核定到账号、卡密状态与账号成功记录不一致的问题。正常成功且上游返回明确匹配邮箱时，充值应自动收口，不要求用户逐笔人工确认。

## 上游协议

- 仅由本机 Python 后端通过 HTTPS 调用 `https://cdk.bar/api/partner/v1`，Basic Auth 用户名固定为 `partner`，密码从进程环境变量 `CDK_PARTNER_PASSWORD` 读取。
- 使用文档列出的八个 POST JSON 操作：`preview`、`preflight`、`redeem`、`result`、`result-by-code`、`lookup`、`lookup-batch`、`billing`。请求路径固定，不提供用户可控 URL 转发，不依赖 `/embed/v1/api/*` 页面接口。
- 每个工作台实例生成并持久化一个格式合规的 `X-Redemption-Device`。工作台未配置 Partner 密码时，充值操作应在触达上游之前明确报配置错误。
- 后端不得记录 Authorization、密码、Session、卡密、兑换 token、预检 token 或完整请求 / 响应体。前端状态、导出和普通操作日志不得包含这些凭证。

## 充值事务与恢复

1. 按文档顺序执行卡密预览、规格核验、账号预检和用户已有的确认流程。预览必须同时返回文档定义的 GPT `product` 和精确套餐 `plan`（Plus 为 `plus`，半价 Plus 为 `plus_half`）；缺字段、产品不为 `gpt`、规格不匹配时，在发送账号 Session 前停止。要求预检返回邮箱与任务目标邮箱一致，且无阻断原因。
2. 发起兑换前生成唯一 `client_request_id`，并将该 ID 与任务、账号、卡密、`redemption_token` 和 `preflight_token` 绑定后持久化。后两个 token 使用现有 Windows DPAPI 加密方法保存在任务 JSON 中；敏感字段不得通过状态接口返回。
3. 提交兑换只允许一次。网络超时、502/504 或 `uncertain` 响应都保持任务为待处理，并先查询同一 redemption token；没有原 token 的旧任务使用 `result-by-code` 按原卡密恢复。禁止换 request ID 重提。
4. 对同一提交轮询结果，正常间隔为文档规定的 3 秒。遇到 429 按退避等待后继续原查询。查询网络错误不改变提交标记、不释放卡密。程序重启后从持久化交易信息恢复查询。
5. `completed` 为兑换成功的必要状态。若结果带有 `account_email`，必须与任务邮箱做 trim 后不区分大小写的精确比较；若结果未带邮箱，再按原卡密调用 `lookup` 获取上游记录的 `account_email`。仅在结果成功且归属邮箱明确匹配时，自动将任务和账号记为成功、当前套餐更新，并将卡密标为已使用。
6. 上游明确成功但邮箱不匹配或仍缺失时，卡密保留为已使用，任务标记归属待核实，不能将 Plus 记到目标账号。非终态状态继续查询；终态失败只有在上游明确 `can_resubmit: true` 时才释放为可重试。`used: false` 单独不能授权重提。
7. 已使用卡密继续显示在卡密管理中，只有用户手动删除才从库存列表归档；充值历史保留。

## 并发登录与超时页

- 十个账号可以一次加入队列，但同时进行的 InPrivate OpenAI 登录最多五个，其余任务排队。Partner API 请求使用独立的低并发闸门；轮询和限流等待不重新提交兑换。
- 登录助手识别截图中的明确 `Operation timed out` 页面及可见重试按钮，仅点击页面上明确的重试动作，单个登录任务最多重试两次并使用短退避。窗口消失、页面不匹配或连续失败时结束该登录任务并释放未提交账号，不把它留在 TOTP 监听状态。
- 页面错误只影响登录事务；已经提交的兑换继续按原 token / 卡密查询，不因窗口关闭而释放卡密或账号的远端交易保护。

## 兼容与数据保护

- 现有未提交任务按现有流程处理。旧版已提交任务没有 Partner token 时，按原卡密调用 `result-by-code` 恢复；无法取得明确成功和邮箱归属时保留待核实状态。
- 任务内部的 Partner 凭证字段必须在 `inventory_public`、`snapshot`、手机网关响应、CSV 导出、错误文本和日志中剔除。
- GPT Session 继续只保存在后台内存，不因本次交易恢复机制写入数据库、升级文件或导出文件。Session 仅在向 Partner `preflight` 发送时使用，不写入任务状态。

## 验证

- 通过模拟 Partner API 验证八个固定操作的 URL、Basic Auth 头、稳定设备 ID、JSON 请求、限流和不确定提交处理；不会调用真实卡密或真实 Session。
- 使用合成账号验证十个任务最多五个同时打开登录页、超时页最多两次重试、页面关闭后释放未提交任务。
- 使用模拟兑换状态覆盖 `queued` / `processing` 后转 `completed`、精确匹配邮箱自动成功、不匹配或缺邮箱进入归属待核实、各终态失败、429、502/504、提交超时和重启后恢复。
- 验证同一任务永不生成第二个兑换请求 ID、同一卡密不被二次提交、成功卡密仍保留在库存、内部凭证不出现在任何前端 / 导出 / 日志面。
- 构建生产前端，更新中文使用说明和版本验证记录。运行中后台如需更新，必须使用现有正常关闭和内存交接机制，保留现有 Session；不得直接杀后台。

## 自审

- 范围限定为 Partner API 适配、原交易恢复、账号归属核验和登录超时处理，没有新增上游接口或卡密重试行为。
- `product`、`plan`、`completed`、`account_email` 和 `can_resubmit` 的用途与 README / OpenAPI 定义一致；缺失数据按未知处理，不推断产品或成功归属。
- 交易 token 与请求 ID 明确区分于 GPT Session；只有交易凭证可加密持久化，Session 保持内存限定。
- 真实上游联调依赖用户在本机启动环境提供 `CDK_PARTNER_PASSWORD`；测试套件使用合成数据，不读取或打印该值。
