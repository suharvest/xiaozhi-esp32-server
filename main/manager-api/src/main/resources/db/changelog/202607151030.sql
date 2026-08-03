--liquibase formatted sql
--changeset xiaozhi:202607151030-1
-- 仓管系统人脸库接口地址（形如 http://host:port/api?key=xxx）
-- key 仅在 manager-api 服务端使用，绝不下发给前端
-- id 取 9xxx 段：这是 fork 自有的参数，上游的 sys_params id 目前在 100~5xx 段递增，
-- 占用相邻 id（如 120）迟早会和上游新增的参数撞号。删除按 param_code 走（该列有
-- unique key），这样无论历史上用过哪个 id 都能幂等重跑，也不会误删上游的行。
DELETE FROM `sys_params` WHERE param_code = 'server.face_warehouse';
INSERT INTO `sys_params` (id, param_code, param_value, value_type, param_type, remark)
VALUES (9101, 'server.face_warehouse', 'null', 'string', 1, '仓管系统人脸库接口地址');
