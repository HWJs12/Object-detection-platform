"""自定义业务异常: 统一承载 "用户可读的错误信息"。"""


class AppError(Exception):
    """业务异常基类。

    Attributes:
        message: 面向用户展示的错误信息
        code: 机器可读错误码, 便于前端分支处理
        status_code: HTTP 状态码
    """

    def __init__(self, message: str, code: str = "ERROR", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class ModelNotConfiguredError(AppError):
    """未找到可用的模型权重文件。"""

    def __init__(self, message: str = "未找到模型权重文件, 请将 .pt 文件放入 models/ 目录"):
        super().__init__(message, code="MODEL_NOT_CONFIGURED", status_code=503)


class ModelLoadError(AppError):
    """模型加载失败。"""

    def __init__(self, message: str = "模型加载失败"):
        super().__init__(message, code="MODEL_LOAD_FAILED", status_code=500)


class InferenceError(AppError):
    """推理过程异常。"""

    def __init__(self, message: str = "模型推理失败"):
        super().__init__(message, code="INFERENCE_FAILED", status_code=500)


class InvalidFileError(AppError):
    """上传文件不合法(类型/大小/内容损坏)。"""

    def __init__(self, message: str = "上传文件不合法"):
        super().__init__(message, code="INVALID_FILE", status_code=400)


class RecordNotFoundError(AppError):
    """记录不存在。"""

    def __init__(self, message: str = "记录不存在"):
        super().__init__(message, code="NOT_FOUND", status_code=404)
