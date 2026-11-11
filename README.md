# GPT Plus Session Workbench

Windows 本地 GPT 账号、Session 和兑换工作台。

## 启动

1. 安装 Python 3.12，并在项目目录执行 `python -m pip install -r requirements.txt`。
2. 按 `使用说明.md` 配置 `CDK_PARTNER_PASSWORD`，不要把密码写入代码或提交到 Git。
3. 双击 `启动工作台.cmd`，再按界面提示连接 Edge 登录助手。

前端已构建到 `web/dist`，开发前端代码位于 `frontend/src`。修改前端后，在 `frontend` 目录执行 `npm install` 和 `npm run build`。

## 数据安全

账号资料、卡密、Session 和交易记录保存在本机数据目录，不在仓库中。Session 和敏感字段使用当前 Windows 用户 DPAPI 加密；请不要把数据库、导出的账号资料、Session 或 Partner API 密码提交到 GitHub。

## 测试

```powershell
python -m pytest tests -q
```
