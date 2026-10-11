"""PBB 名字评分测号器。

包内分层（详见仓库根的 REPO.md）：

- ``config_schema``   配置字段与默认值，config→engine 键名映射的唯一真相源
- ``build_settings``  本机构建选项（build_config.json）的读写与有效性判定
- ``engine``          C++ 引擎子进程的参数构建、输出解析与结果拆分
- ``main``            CLI 编排层：解析配置/命令行并驱动 engine
- ``ui``              图形监控器
- ``autotune``        本机自动测速（局部搜索 + 正确性对拍）
- ``paths``           全项目唯一的路径解析入口

``build.py`` 等构建/打包工具**不在包里**，它们在 ``tools/``，运行方式见 REPO.md。

入口脚本（仓库根）只是薄壳：
``main.py``（CLI）、``ui.py``（图形监控器）、``tools/build.py``（编译）、
``tools/tune.py``（自动测速）、``tools/package_release.py``（打包）。
"""

__all__ = [
    "autotune",
    "build_settings",
    "config_schema",
    "engine",
    "main",
    "paths",
    "ui",
]
