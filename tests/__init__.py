"""测试包：按 Runner 的方式把插件目录注册成 ``emoji_label_bridge`` 包。

Runner 加载插件时用 ``spec_from_file_location`` + ``submodule_search_locations``
把插件目录当作包，插件的相对导入才成立。这里照同样的方式注册固定包名，
所以本地 ``plugins/emoji_label_bridge`` 与 CI 的仓库根目录都能跑。

测试不导入宿主 ``src.*``，不依赖 pytest，也不需要网络。
"""

from __future__ import annotations

from pathlib import Path

import importlib.util
import sys

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "emoji_label_bridge"

if PACKAGE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if _spec is None or _spec.loader is None:
        raise ImportError(f"无法为插件目录创建模块 spec: {PLUGIN_DIR}")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[PACKAGE_NAME] = _module
    _spec.loader.exec_module(_module)
