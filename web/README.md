# CodeAgent Web

本机 CodeAgent 的 React 工作台。开发服务器会把 `/api` 代理到 `http://127.0.0.1:8765`，生产构建输出到 `web/dist/`，由后端同源托管。

```powershell
npm.cmd install
npm.cmd run dev
```

如需修改开发代理目标，请调整 `vite.config.ts`。前端不读取任何模型密钥；运行时信息仅来自脱敏的 `/api/runtime-config`。
