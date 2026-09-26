"""API 运行日志配置：同时输出控制台，并按文件总容量保存 Uvicorn 日志。"""

from __future__ import annotations

from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from config_loader import get_runtime_root, load_jsonc, resolve_runtime_path


class TotalSizeRotatingFileHandler(RotatingFileHandler):
    """单文件滚动后清理最旧归档，使日志目录不超过指定总容量。"""

    def __init__(
        self,
        filename: str,
        mode: str = "a",
        maxBytes: int = 10 * 1024 * 1024,
        backupCount: int = 20,
        encoding: str | None = "utf-8",
        delay: bool = False,
        totalSizeBytes: int = 100 * 1024 * 1024,
    ) -> None:
        """创建带目录总容量限制的日志处理器。

        Args:
            filename: 当前日志文件的绝对路径。
            mode: 文件写入模式；默认追加。
            maxBytes: 单个日志文件达到该字节数后滚动。
            backupCount: 最多保留的归档文件数上限。
            encoding: 日志文件编码。
            delay: 是否延迟至首次写入时创建文件。
            totalSizeBytes: 当前日志及其归档的累计容量上限。
        """
        self.total_size_bytes = max(1, int(totalSizeBytes))
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(filename, mode, max(1, int(maxBytes)), max(1, int(backupCount)), encoding, delay)
        self._trim_archives()

    def doRollover(self) -> None:
        """滚动当前日志，并在滚动后删除最旧归档以满足总容量限制。"""
        super().doRollover()
        self._trim_archives()

    def emit(self, record: Any) -> None:
        """写入日志后检查总容量，覆盖滚动后的新文件写入场景。

        Args:
            record: Python logging 创建的单条日志记录。
        """
        super().emit(record)
        self._trim_archives()

    def _trim_archives(self) -> None:
        """按修改时间从旧到新清理超过容量上限的日志归档。"""
        base_path = Path(self.baseFilename)
        candidates = [path for path in base_path.parent.glob(f"{base_path.name}*") if path.is_file()]
        total_size = sum(path.stat().st_size for path in candidates)
        # 当前正在写入的日志不能删除；优先清理已滚动的最旧归档。
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime):
            if total_size <= self.total_size_bytes:
                break
            if path.resolve() == base_path.resolve():
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total_size -= size


def _load_log_options() -> Dict[str, Any]:
    """从外置仓库配置读取 API 日志选项，并对错误配置回退至默认值。

    Returns:
        标准化后的日志目录、文件名与容量参数。
    """
    try:
        config = load_jsonc(resolve_runtime_path("config/warehouse.json"))
        raw_options = config.get("api_logging", {}) if isinstance(config, dict) else {}
    except (OSError, ValueError, TypeError):
        raw_options = {}

    options = raw_options if isinstance(raw_options, dict) else {}
    directory = str(options.get("directory", "logs")).strip() or "logs"
    file_name = str(options.get("file_name", "api.log")).strip() or "api.log"
    return {
        "enabled": bool(options.get("enabled", True)),
        "path": get_runtime_root() / directory / file_name,
        "max_bytes": max(1, int(float(options.get("max_file_size_mb", 10)) * 1024 * 1024)),
        "total_bytes": max(1, int(float(options.get("max_total_size_mb", 100)) * 1024 * 1024)),
        "backup_count": max(1, int(options.get("backup_count", 20))),
    }


def create_uvicorn_log_config() -> Dict[str, Any] | None:
    """生成 Uvicorn 使用的控制台与容量受限文件日志配置。

    Returns:
        日志禁用时返回 ``None``；否则返回 Uvicorn 配置字典。
    """
    options = _load_log_options()
    if not options["enabled"]:
        return None

    file_handler = {
        "class": "api.logging_setup.TotalSizeRotatingFileHandler",
        "level": "INFO",
        "formatter": "default",
        "filename": str(options["path"]),
        "maxBytes": options["max_bytes"],
        "backupCount": options["backup_count"],
        "totalSizeBytes": options["total_bytes"],
        "encoding": "utf-8",
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s [%(name)s] %(message)s"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "file": file_handler,
        },
        "loggers": {
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            # 路由仅记录任务、巷道和货位等排障摘要，不写入完整库存快照或请求体。
            "api.business": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        },
    }
