import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


def configure_global_logging(
    service_name: str = "adapt_fetch",
    log_dir: str = None,
    level: str = None,
    backup_count: int = 14,
    max_bytes: int = 50 * 1024 * 1024,  # 50MB 每个日志文件
    formatter: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
):
    """配置全局日志：控制台输出（不写入文件）
    
    ⚠️ 配置 root logger：捕获当前进程内所有模块的日志
    由于本服务独立运行（python start_unified.py），不会影响其他 Python 脚本

    环境变量（可选覆盖参数）：
      - APP_LOG_LEVEL: 日志级别，默认 INFO
      - APP_LOG_MAX_BYTES: 日志文件大小上限（字节/如 "50MB"），默认 50MB
      - APP_LOG_BACKUP: 轮转备份数，默认 14
    
    💡 默认只输出到控制台，避免文件 I/O 阻塞影响服务性能
    """

    env_level = (os.getenv("APP_LOG_LEVEL") or level or "INFO").upper()
    enable_file_logging = os.getenv("ENABLE_FILE_LOGGING", "false").lower() == "true"

    # 确保标准输出无缓冲，避免日志刷新延迟
    try:
        os.environ.setdefault("PYTHONUNBUFFERED", "1")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

    # 配置 root logger：捕获当前进程内所有日志
    # 由于本服务是独立进程运行，不会影响其他 Python 脚本
    target_logger = logging.getLogger()  # root logger
    
    # 清理已存在的处理器，避免重复输出
    for h in list(target_logger.handlers):
        target_logger.removeHandler(h)

    target_logger.setLevel(getattr(logging, env_level, logging.INFO))

    fmt = logging.Formatter(formatter)

    # 控制台输出（stdout）- 始终启用
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(target_logger.level)
    console_handler.setFormatter(fmt)
    target_logger.addHandler(console_handler)

    log_file = None
    
    # 文件日志（可选，默认禁用）
    if enable_file_logging:
        env_log_dir = os.getenv("APP_LOG_DIR", log_dir or "logs")
        env_backup = int(os.getenv("APP_LOG_BACKUP", str(backup_count)))
        
        # 解析 APP_LOG_MAX_BYTES（支持纯数字/KB/MB）
        env_max_bytes = max_bytes
        max_bytes_str = os.getenv("APP_LOG_MAX_BYTES", "")
        if max_bytes_str:
            try:
                s = max_bytes_str.strip().lower()
                if s.endswith("mb"):
                    env_max_bytes = int(float(s[:-2]) * 1024 * 1024)
                elif s.endswith("kb"):
                    env_max_bytes = int(float(s[:-2]) * 1024)
                else:
                    env_max_bytes = int(s)
            except Exception:
                pass

        Path(env_log_dir).mkdir(parents=True, exist_ok=True)
        log_file = os.path.join(env_log_dir, f"{service_name}.log")
        
        # 文件轮转处理器（按大小轮转，Windows 兼容性好）
        file_handler = RotatingFileHandler(
            log_file, 
            maxBytes=env_max_bytes, 
            backupCount=env_backup, 
            encoding="utf-8"
        )
        file_handler.setLevel(target_logger.level)
        file_handler.setFormatter(fmt)
        target_logger.addHandler(file_handler)
        print(f"✅ 文件日志已启用: {log_file}")
    else:
        print("ℹ️  文件日志已禁用（仅控制台输出），如需启用请设置环境变量: ENABLE_FILE_LOGGING=true")

    # 配置常见库日志，让它们也使用相同的配置
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "granian"):
        lg = logging.getLogger(name)
        lg.setLevel(target_logger.level)
        lg.propagate = True  # 传播到 root logger
        # 清除它们自己的 handlers，避免重复输出
        for h in list(lg.handlers):
            lg.removeHandler(h)
    
    logging.captureWarnings(True)

    return {
        "log_file": log_file if enable_file_logging else "disabled",
        "level": target_logger.level,
        "namespace": "root（全局）",
        "mode": "控制台输出" + (" + 文件持久化" if enable_file_logging else "（文件日志已禁用）")
    }


