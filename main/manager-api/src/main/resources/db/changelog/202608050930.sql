-- liquibase formatted sql
-- changeset xiaozhi:202608050930-1
-- 预置默认管理员账号，让设备下发后开机即可登录、无需先注册。
--
--   用户名：admin
--   初始密码：Seeed@2026
--
-- ⚠️ 这是**公开的默认密码**，必须在交付文档和首次登录引导里要求客户立刻修改。
-- 智控台自带修改密码功能（右上角账号菜单 → 修改密码，ChangePasswordDialog.vue）。
--
-- 为什么要预置：
-- 上游逻辑是「第一个注册的用户自动成为超管」（SysUserServiceImpl.java:85-90，
-- userCount == 0 时 setSuperAdmin(YES)）。不预置的话，部署脚本拿不到任何登录凭据，
-- 也就没法在部署完成后自动写入模型地址、MCP 接入点等配置——所有配置都得靠客户
-- 照着说明手工点一遍。预置之后 actions.after 才可能真正做到开箱即用。
--
-- 副作用（是期望的）：userCount 从此不为 0，后续注册的用户都是普通用户而非超管。
-- 另外 server.allow_user_register 默认为 false，交付形态下注册入口本来就是关的。
--
-- 密码哈希说明：本项目用的是经典 jBCrypt，BCryptPasswordEncoder 的正则只接受
-- $2$ / $2a$（BCryptPasswordEncoder.java:21-22），且 hashpw 显式拒绝 'a' 以外的
-- minor 版本。macOS 自带 htpasswd 产出的是 $2y$，写进去会被判定「不像 bcrypt」而
-- 登录必失败——生成哈希时务必指定 2a。
--
-- id 取 9xxx 段：与 sys_params 的做法一致，避开上游雪花 id 的取值范围。

DELETE FROM `sys_user` WHERE username = 'admin';
INSERT INTO `sys_user` (id, username, password, super_admin, status, creator, create_date, updater, update_date)
VALUES (
  9001,
  'admin',
  '$2a$10$NFmyaieTptAUW8hi9CTRKe8oAZ8S/Xu5I9ynbtzJQ126D9PeeQGI6',
  1,
  1,
  1, NOW(), 1, NOW()
);
