import sys, os, io
from loguru import logger

# 获得当前项目的绝对路径
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(root_dir, "logs")  # 存放项目日志目录的绝对路径

if not os.path.exists(log_dir):  # 如果日志目录不存在，则创建
    os.mkdir(log_dir)

# Trace < Debug < Info < Success < Warning < Error < Critical

# 在 Windows 環境下，強制將 sys.stdout 和 sys.stderr 設為 utf-8 編碼
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and getattr(_stream, "encoding", None) != "utf-8":
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

class MyLogger:
    def __init__(self):
        self.logger = logger
        # 清空所有設定
        self.logger.remove()

        # 終端機保持乾淨，只顯示 INFO 等級以上的訊息
        self.logger.add(sys.stdout, level='INFO',
                        format="{time:YYYY-MM-DD HH:mm:ss} | "  # 時間
                               "{process.name} | "  # 進程名
                               "{thread.name} | "  # 線程名
                               "{module}.{function}"  # 模組名.方法名
                               ":{line} | "  # 行號
                               "{level}: "  # 等級
                               "{message}",  # 日誌內容
                        )
        # 輸出到檔案，讓中間過程可以被保留查看
        log_file_path = os.path.join(log_dir, "text_to_sql2.log")
        self.logger.add(log_file_path, level='DEBUG', encoding='UTF-8',
                        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
                        rotation="10 MB",  # 檔案太大時自動切割
                        retention="7 days" # 保留 7 天
                        )

    def get_logger(self):
        return self.logger

log = MyLogger().get_logger()

if __name__ == '__main__':
    # log.debug("This is a debug message.")
    # log.info("This is an info message.")
    # log.warning('这是一个警告')
    # log.trace('xxxx')
    print('str.pdf'['str.pdf'.rindex('.'):])
    # @log.catch  # 整个函数自动加上try, catch。自动捕获异常，并且通过日志打印
    def test():
        try:
            print(3/0)
        except ZeroDivisionError as e:
            # log.error(e)
            log.exception(e)  # 以后常用