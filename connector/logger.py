import logging
import sys
from .config import LOG_CONFIG

def setup_logger(name: str):
    logger = logging.getLogger(name)
    
    # Avoid duplicate handlers if setup_logger is called multiple times for the same name
    if logger.handlers:
        return logger
        
    logger.setLevel(LOG_CONFIG["level"])
    # 不向 root 传播：否则一旦某个依赖调用过 logging.basicConfig()，
    # 每条日志会被 root 的 handler 再打印一遍（表现为日志双份）
    logger.propagate = False
    
    formatter = logging.Formatter(LOG_CONFIG["format"])
    
    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File Handler
    try:
        file_handler = logging.FileHandler(LOG_CONFIG["file"], encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"Failed to set up file logging: {e}")
        
    return logger
