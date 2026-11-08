# 项目维护

项目入口是根目录的`AigisConsole.exe`和启动脚本；源码、客户端工具、规则示例、说明和测试分别在`src`、`client`、`config`、`docs`和`tests`。`runtime`包含实际资源和玩家数据，不能整体删除。`artifacts`保留报告及部分测试依赖，`tmp`同时包含测试临时文件和研究材料，也不能按目录名整体清空。

`artifacts/cdn-contract-20261101`中的历史索引仍被测试直接读取；`artifacts/import-review-dc0ef7ca`含目录联接，不能随其他生成物直接递归清理。

新测试临时目录放在`tmp`下，需要长期保存的结果记录在`artifacts`或相应文档中。`.cache`、pytest/ruff缓存、Python字节码和可重建的`AigisConsole.exe`已列入Git忽略；生成EXE使用`build-console.ps1`。
