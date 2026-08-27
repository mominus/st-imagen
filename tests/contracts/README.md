# 后台 API 契约

`admin_api.json` 是后台控制台与 FastAPI 管理路由之间的版本化契约，记录：

- HTTP 方法和稳定路径；
- 路由是否必须经过 `require_admin`；
- 账号、邀请码和用户列表依赖的稳定字段。

`tests/test_admin_api_contract.py` 会同时检查路由实现、前端实际引用和资源序列化结果。
修改后台 API 时应先确认兼容性，再显式更新契约版本或内容；新增接口也需要加入契约，避免未经审查的接口漂移。
