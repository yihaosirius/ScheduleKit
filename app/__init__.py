"""ScheduleKit 服务端应用包。

只导出 ``__version__``，其余一律通过子模块显式导入：
``from app.services import tasks`` 而不是 ``from app import tasks``——
这样依赖方向在 import 语句里一眼可见。
"""

__version__ = "0.1.0"
