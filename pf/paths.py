"""插件数据目录。"""

from __future__ import annotations

import os
from pathlib import Path

from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_PersonaFlow"
VERSION = "2.1.0"


def get_data_dir() -> Path:
    """返回插件数据目录 data/plugin_data/astrbot_plugin_PersonaFlow。

    设置了环境变量 PERSONAFLOW_DATA_DIR 时改用它（调试或迁移演练时用）。
    """
    override = os.environ.get("PERSONAFLOW_DATA_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(StarTools.get_data_dir(PLUGIN_NAME))
