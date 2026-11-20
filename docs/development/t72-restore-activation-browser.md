# 恢复启用后的浏览器验证

日期：2026-09-15。状态：**本次范围通过**。覆盖已启用副本的范围提示、工作台草稿保存、新进程重启读回和跨作品拒绝；不代表完整恢复向导、浏览器正式发布或整个 T7.2 已交付。

## 环境与准备

- Chrome DevTools MCP，page 2，隔离浏览器上下文 `restore-activation-final`。
- 地址 `http://127.0.0.1:38893/`，真实 FastAPI/SQLite 工作台。
- 独立合成恢复容器 `.tmp/rm-c5ac8f`，机器记录 `.tmp/rm-c5ac8f-machine`；原合成作品仍存在，确保覆盖此前“旧源目录重新出现”问题。
- 44091 exit 0：从实际归档依次恢复、重绑定、交接、预算/任务/执行隔离、启用，记录 `.tmp/t72-activation-browser-prepared.json`。
- 合成第三章候选供浏览器编辑；SDK 构造和非本地连接被测试服务器禁止，启动执行器均使用拒绝函数。未调用真实 Provider、未写真实作品。

## 操作及结果

1. 打开第三章页面，无缓存刷新。页面显示“恢复副本已启用，只访问目标作品。”；维护、归档、新建、导入控件隐藏。bootstrap 返回 `state=active/read_only=false`，项目列表只包含目标作品。
2. 使用原生填充和点击修改“正文草稿”并点击“保存工作台草稿”。`PUT /api/candidate-drafts/candidate_draft_8bcd318eba2a48e89747f7b959ddd7af` 返回 200，revision 1→2，SHA 为 `cb06b080847fba36751838e77006f92a229e372ddee5f10d6e7ec6688e6c877f`。页面显示“所有修改已保存”。
3. 通过停止文件正常关闭服务 54151/PID32920（exit 0），启动新服务 85971/PID16472。重新加载页面，从候选详情 GET 响应和可见编辑器均读回相同作者文字、revision 2、SHA；没有额外模型执行。
4. 在浏览器导航到另一登记作品的概览。目标请求返回 409，正文显示“此恢复副本只包含目标作品，其他登记作品不可访问。”，没有显示另一作品私有草稿。
5. 通过停止文件关闭第二个服务（85971 exit 0），确认 38893 无监听。

## 控制台与网络

保存及重启读回期间没有 `error` 或 `warn`。基线已有一条表单字段缺少 id/name 的浏览器 issue，发生在操作前，未新增。

正常流程 bootstrap、项目、章节、章卡、候选、事实列表、预算 GET 均返回 200；保存 PUT 返回 200，响应中 revision/SHA/text 与 UI 一致。跨作品请求返回预期 409，控制台对应资源加载错误属于本次负向测试。未记录 Cookie、令牌或请求头。

## 证据与限制

- `.tmp/t72-activation-browser-evidence.json`：实际新进程读回、HTTP 响应正文、范围拒绝和可见页面快照。
- `.tmp/t72-activation-browser-new.log`、`.tmp/t72-activation-browser-restart.log`：两个实际服务的 PID/任务目录及正常结束记录。
- `.tmp/t72-activation-browser-after.json`：`book_unchanged=true`、`usage_unchanged=true`、`worker_thread_absent=true`、`calls=[]`。
- `.tmp/t72-activation-browser-info.json`：目标 ID、物理位置、工作台草稿列表；未保存认证信息。

此前旧样本实际保存 409 的失败证据仍保留；本次使用绑定旧终态历史的新恢复样本验证修复。尚未覆盖浏览器恢复选择/启用向导、自动批次或正式发布确认。早期发布勾选的自动审批 credits 拒绝记录仍有效，不能由本次草稿保存验证替代。
