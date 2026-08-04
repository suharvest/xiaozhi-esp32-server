-- liquibase formatted sql
-- changeset xiaozhi:202608041730-1
-- 撤销 server.face_warehouse 参数：智控台的人脸库管理面板已移除。
--
-- 原因：人脸录入本来就是仓管系统的原生功能（它有完整的录入 UI、去重、20 张上限、
-- model_tag 一致性、租户隔离），在智控台再做一层代理只是把同一件事换个地方点，
-- 却引入了额外的依赖：仓管系统没起来 / 地址没配 / API Key 权限不够（那 7 个端点
-- 都要 FACE/ADMIN，而 MCP 连接自动建的 key 是 role=operate），面板就是废的。
-- 结论是直接用仓管系统自己的配置页，智控台不掺和。
--
-- 202607151030 里插入这一行的 changeset 保持不动（仓库约定：只允许新建 changeSet，
-- 不允许修改已执行的），这里只做数据清理。按 param_code 删除，与当初的写法对称。

DELETE FROM `sys_params` WHERE param_code = 'server.face_warehouse';
