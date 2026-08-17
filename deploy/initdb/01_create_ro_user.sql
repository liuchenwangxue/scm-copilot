-- ★ W24 Day1：scm_biz 业务库 + NL2SQL 只读沙箱账号（与 sqlglot 四道闸构成纵深防御双保险）
-- 说明：
--   - 挂载到 docker-entrypoint-initdb.d 由 MySQL 首次初始化时执行；
--     数据卷非空时不会重复执行（改密码需 docker compose down -v 重建，会清空平台库，注意顺序）
--   - CREATE DATABASE IF NOT EXISTS scm_biz：compose 的 MYSQL_DATABASE 只建 scm_platform，
--     业务库在这里补建（utf8mb4 与平台库同字符集）
--   - 仅授 SELECT，无 UPDATE/DELETE/INSERT/DDL/锁表权限——即使模型生成恶意 SQL
--     且四道闸有未知绕过，数据库权限层仍兜底拒绝（ERROR 1142）
--   - 密码为 dev 环境默认值，写入 docs 开发文档（非生产密码）

CREATE DATABASE IF NOT EXISTS scm_biz CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'nl2sql_ro'@'%' IDENTIFIED BY 'ro_pass_2026_dev';
GRANT SELECT ON scm_biz.* TO 'nl2sql_ro'@'%';
FLUSH PRIVILEGES;
