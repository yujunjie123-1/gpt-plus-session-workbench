# 无痕登录隔离调查（2026-11-07）

此文件记录 v2.1.0 的调查结论。v2.2.0 已按独立资料目录方案接入，并完成本机原生窗口隔离验证；当前实现、自动准备权限和任务绑定见 isolated-batch-design.md。下文“本次范围”指此前调查阶段。

可以让多个窗口分别登录不同 GPT 账号。普通的新建无痕窗口会延续同一个无痕会话，需要分别隔离浏览器资料目录。

## 推荐接入方式

为每个账号分配独立、绝对路径的 Edge user-data-dir，再使用 InPrivate 窗口。各个浏览器实例使用不同资料目录，Cookie 与站点数据互不混用；仅增加 --new-window 不提供这种隔离。Microsoft 官方确认 Edge 支持用 --user-data-dir 覆盖默认目录；企业 UserDataDir 策略可能覆盖该参数，应读取 edge://version 的实际路径验收。

采用工作台专用目录，例如 Windows 用户目录下 GPTPlusWorkbench/browser-profiles/账号内部ID，不复制用户日常浏览器资料。Session 仍不写入工作台数据库；浏览器自身配置、扩展和缓存按各目录维护，InPrivate 站点数据的生命周期由浏览器负责。

保留现有原生 Edge、无远程调试的登录路径，有助于沿用现在的助手。但每个资料目录需要配置并允许助手在 InPrivate 中使用。接入前还需增加“任务—浏览器资料目录—助手实例”绑定，防止两个助手同时填写同一任务，并在后台按任务所属实例分别读取 Session。仅改启动参数不能完整解决任务串号问题。

## 其他方式

WebView2 官方支持为多个 ProfileName 保存独立的 Cookie、设置和缓存，也支持 IsInPrivateModeEnabled。它可以做成工具内嵌窗口，代价是重做登录与 Session 交接，并额外验收真实 GPT 登录。

Playwright BrowserContext 能隔离 Cookie，但此项目此前在受控登录浏览器中遇到过 400。因此浏览器隔离能力与真实 GPT 登录成功必须分别验证，不能因本地 Cookie 分开就承诺登录服务一定接受。

## 本次范围与验收

本次完成官方资料核对和接入设计，没有更换现有真实登录方式、启动新的真实 GPT 登录、提交密码或验证码。后续接入应验证两个实例的实际资料路径、Cookie 隔离、两个模拟账号的 Session 归属、助手路由与关闭清理，再单独验证真实登录。

- Chrome 官方：另开一个无痕窗口时，当前无痕会话会在新窗口继续。https://support.google.com/chrome/answer/95464?hl=en
- Chromium 官方：用户资料目录包含 Cookie 等数据，支持 --user-data-dir。https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md
- Microsoft Edge 官方：UserDataDir 策略及命令行覆盖规则。https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/userdatadir
- Microsoft WebView2 官方：多 Profile 独立数据与 InPrivate 配置。https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/multi-profile-support
