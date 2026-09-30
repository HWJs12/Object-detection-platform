# 基于深度学习的水下海洋生物目标检测与分析系统

基于 GSCNet 的水下海洋生物检测 Web 系统。上传图片或视频 → 模型推理 → 显示检测框与置信度 →
统计目标数量 → 保存记录 → 生成统计图表。后端 FastAPI + SQLite，前端原生 HTML/CSS/JS + Chart.js，
视频检测通过 WebSocket 实时推流。

---

## 项目当前状态（已用真实模型跑通）

| 项目 | 内容 |
| --- | --- |
| 权重文件 | `models/best.pt`（YOLOv11，5 类：`hd` / `hx` / `hs` / `sb` / `other`） |
| 训练配置 | `models/data.yaml`（原始训练配置，仅供查阅，程序运行不依赖它） |
| 测试素材 | `samples/`：5 张实测图片 + `MyVideo_2.mp4`（1920×1080 / 25fps / 302 帧）+ `sample_underwater.mp4`（120 帧小视频，CPU 上 10 秒内就能跑完，适合快速演示视频功能） |
| 一键启动 | 双击 `start.bat` |
| 数据库 | 已清空，首次使用从 0 条记录开始统计 |

**实测结果（CPU 推理，非估算）**

| 场景 | 实测值 |
| --- | --- |
| 模型加载 | 8.0 s（启动时后台预热，不阻塞页面打开） |
| 单张图 720×405 | 33 ~ 47 ms／张（例：`YN030001_12537.jpg` 检出 5 个 `hd`） |
| 单张图大尺寸 | 约 150 ms／张 |
| 视频 1920×1080（缩放 960、frame_skip=2） | 处理 151 帧／19.9 s ≈ 7.6 FPS，实时推送 146 帧 |
| 视频统计结果 | 单帧峰值 4 个、平均每帧 2.1 个，类别累计 hd 180 / sb 130 / hs 4 / hx 3 |

---

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11（Linux、macOS 亦可） |
| Python | 3.9 ~ 3.13（推荐 3.11；本项目实测 3.13.14） |
| 显卡 | 无要求，默认 CPU 推理；检测到 CUDA 会自动使用 GPU |
| 磁盘 | 约 4 GB（PyTorch CPU 版约 2 GB） |
| 浏览器 | Edge / Chrome 等现代浏览器 |

---

## 二、安装步骤

### 1. 创建并激活虚拟环境

```bat
cd D:\workbuddy\2026-09-30-09-41-47\underwater-detection
python -m venv .venv
.venv\Scripts\activate
```

### 2. 安装 PyTorch（CPU 版，无 NVIDIA 显卡时必须用这个源，否则会装 2GB+ 的 CUDA 包）

```bat
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

> 若下载慢，可先执行 `pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/`，
> 但 **torch 仍建议使用上面的官方 CPU 源**。

### 3. 安装其余依赖

```bat
pip install -r requirements.txt
```

### 4. 放入模型权重

把训练好的 `.pt` 文件复制到 `models/` 目录，例如 `models/best.pt`。
程序会自动扫描该目录（详见 `models/README.md`）。

---

## 三、启动系统

### 方式一：双击 `start.bat`（最简单）

会自动寻找 Python 环境并启动服务，然后浏览器打开 **http://127.0.0.1:8000**

### 方式二：命令行

```bat
cd D:\workbuddy\2026-09-30-09-41-47\underwater-detection

:: 本机已配置好的虚拟环境（推荐，依赖已装齐）
C:\Users\12503\.workbuddy\binaries\python\envs\underwater\Scripts\python.exe run.py

:: 或使用自己创建的环境
python run.py
```

默认地址：**http://127.0.0.1:8000**

可选参数：

```bat
python run.py --port 8080        :: 指定端口（端口被占用时会自动向后寻找可用端口）
python run.py --host 0.0.0.0     :: 允许局域网内其它设备访问
python run.py --reload           :: 开发模式，代码修改后自动重启
```

启动成功后浏览器会自动打开首页即可使用；若未自动打开，手动访问上面的地址。

---

## 四、功能与页面

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| 首页 | `/` | 系统概览、模型信息、最近记录 |
| 图片检测 | `/detect_image.html` | 上传图片，显示原图 + 带检测框结果图 + 统计 + 明细表 |
| 视频检测 | `/detect_video.html` | 上传视频，逐帧检测并实时显示画面、当前帧目标数、各类别数量、FPS；支持**暂停 / 继续**检测，并可在检测过程中**实时拖动调节置信度阈值** |
| 检测记录 | `/records.html` | 历史记录表格，支持按类型 / 文件名 / 日期筛选、查看详情、删除 |
| 数据统计 | `/stats.html` | 总览卡片 + 柱状图 / 折线图 / 饼图 / 环形图（数据全部来自数据库） |

### 典型使用流程

**图片检测**：打开「图片检测」→ 点击/拖拽上传图片 → 可调置信度阈值 → 点「开始检测」→
左侧显示原图、右侧显示带检测框的结果图 → 下方显示目标总数、推理耗时、各分类数量、逐目标明细。

**视频检测**：打开「视频检测」→ 上传视频 → **拖动滑动条设定置信度阈值**（0.05~0.95，
值越低保留的候选框越多）→ 点「开始检测」→ 画面实时逐帧刷新并叠加检测框，下方实时更新
「当前帧目标数量 / 各类别数量 / FPS / 播放时间 / 单帧峰值」。

检测过程中可以随时：
- 点「**暂停检测**」→ 画面叠加“已暂停”遮罩，服务端停止读帧与推送，**保留当前播放位置**；
- 点「**继续检测**」→ 从暂停位置接着处理，帧号连续，统计继续累计；
- **拖动滑动条** → 阈值立即对后续帧生效（例如从 0.5 调到 0.15 可发现更多小目标）；
- 点「停止检测」→ 中断本次任务（不保存记录）。

检测结束后展示全程汇总（处理帧数、平均速度、单帧峰值、平均每帧目标数、各类别累计数量），
记录自动入库。

**查看历史**：打开「检测记录」→ 按类型 / 文件名 / 日期筛选 → 点「详情」查看结果图、
类别统计、逐帧采样数据与目标明细 → 也可删除单条记录。

**查看统计**：打开「数据统计」→ 查看今日/累计检测次数、目标总数、类别数 → 切换 7/14/30 天
趋势 → 柱状图（每日检测次数）、折线图（每日检出目标数）、饼图（类别占比）、环形图（图片/视频占比）。

---

## 五、模型与配置

所有参数集中在 `config.py` 与可选的根目录 `config.yaml`，代码中没有硬编码的路径或阈值。

配置优先级：**环境变量 > config.yaml > 默认值**。

常用配置项（复制 `config.example.yaml` 为 `config.yaml` 后修改即可）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `model_path` | `models/` 下第一个 `.pt` | 权重文件路径 |
| `conf_threshold` | 0.25 | 置信度阈值 |
| `iou_threshold` | 0.45 | NMS IoU 阈值 |
| `imgsz` | 640 | 模型输入尺寸 |
| `device` | auto | auto / cpu / cuda / 0 |
| `frame_skip` | 2 | 视频每 N 帧检测 1 帧（CPU 提速关键） |
| `video_max_side` | 960 | 视频帧长边缩放上限 |
| `jpeg_quality` | 70 | WebSocket 推流图像质量 |
| `push_fps_limit` | 20 | 推流帧率上限，0 表示不限 |
| `max_upload_mb` | 300 | 单文件上传大小上限 |
| `class_name_map` | `{}` | 类别**显示名**映射（可选，见下方说明） |

### 类别名与中文显示（可选）

类别名始终从模型自身读取（`model.names`），当前权重返回的是 `hd / hx / hs / sb / other`，
代码中没有写死任何海洋生物类别。

如果想让页面显示中文名，复制 `config.example.yaml` 为 `config.yaml`，填上映射即可：

```yaml
class_name_map:
  hd: 海胆
  hx: 海星
  hs: 海参
  sb: 扇贝
  other: 其他
```

> 上面的中文对应关系是按 URPC 常见标注推测的，请以你自己数据集 `data.yaml` 的标注含义为准。
> 留空或不配置 = 直接显示模型原始类别名。

环境变量示例：`MODEL_PATH`、`CONF_THRESHOLD`、`IMGSZ`、`DEVICE`、`FRAME_SKIP`、`PORT`。

运行时调参：前端图片检测页可直接改置信度；也可调用 `PUT /api/config`（进程内生效）。

---

## 六、目录结构

```
underwater-detection/
├── config.py                 # 集中配置（路径/阈值/服务参数）
├── config.example.yaml       # 配置覆盖示例
├── run.py                    # 启动入口
├── start.bat                 # Windows 一键启动
├── requirements.txt
├── README.md
├── samples/                  # 测试素材(5 张实测图 + 1 段视频)
├── models/                   # 权重文件(best.pt / data.yaml)
├── data/                     # 运行时数据（自动创建）
│   ├── uploads/              # 上传的图片 / 视频
│   ├── results/              # 检测结果图
│   └── app.db                # SQLite 数据库
├── app/
│   ├── main.py               # FastAPI 入口、异常处理、静态挂载
│   ├── api/                  # detect(检测/WS) records(记录) stats(统计) model(模型/配置)
│   ├── core/                 # yolo_engine(推理) detection_service(图片) video_task(视频)
│   ├── db/                   # database(连接) dao(SQL) schema.sql(表结构)
│   └── utils/                # 文件/图像/异常/响应 工具
└── web/
    ├── index.html detect_image.html detect_video.html records.html stats.html
    ├── css/style.css
    ├── js/common.js home.js image.js video.js records.js stats.js
    └── vendor/chart.js        # Chart.js 本地化，离线可用
```

数据库表：`detections`（主表）、`detection_items`（目标明细）、
`detection_class_stats`（类别统计）、`video_frame_stats`（视频逐帧统计）。

---

## 七、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/model/info` | 模型信息与类别列表 |
| POST | `/api/model/reload` | 重新加载模型 |
| GET / PUT | `/api/config` | 读取 / 更新运行时配置 |
| POST | `/api/detect/image` | 图片检测（multipart，可选 conf） |
| POST | `/api/detect/video` | 上传视频，返回 task_id（可选 conf 指定本次阈值） |
| POST | `/api/detect/video/{task_id}/control` | 控制视频任务：暂停 / 继续 / 运行中调整阈值 |
| WS | `/ws/video/{task_id}` | 视频逐帧实时检测推送 |
| GET | `/api/records` | 记录列表（分页 / 类型 / 关键字 / 日期） |
| GET | `/api/records/{id}` | 记录详情（含明细、类别统计、帧统计） |
| DELETE | `/api/records/{id}` | 删除记录 |
| GET | `/api/stats/overview` | 总览统计 |
| GET | `/api/stats/trend?days=7` | 每日趋势 |
| GET | `/api/stats/classes` | 各类别数量 |
| GET | `/api/stats/dashboard` | 统计页聚合数据 |

WebSocket 推送消息：`{"type":"frame", image, total, counts, detections, fps, progress}`、
`{"type":"done", summary}`、`{"type":"error", message}`。

视频任务控制接口的请求体：

```json
{"action": "pause"}                    // 暂停检测
{"action": "resume"}                   // 继续检测
{"action": "set_conf", "value": 0.15}  // 运行中调整置信度阈值(0.01~0.99)
```

返回当前任务状态：`{"task_id", "paused", "frame_index", "processed", "conf_threshold", "total_frames"}`。

---

## 八、性能与优化

测试环境：Windows 11 / CPU 推理（无 NVIDIA 显卡）/ 权重 `models/best.pt`（YOLOv11，imgsz 640）。
真实实测数据见本文开头「项目当前状态」，均为实际运行采集，未使用任何估算或虚构数值。

CPU 较慢时的优化方式（只改配置，不用改代码）：

| 目标 | 调整方式 |
| --- | --- |
| 视频更快 | 增大 `frame_skip`（2 → 3~5，跳帧更多，统计仍实时刷新） |
| 单帧更快 | 减小 `imgsz`（640 → 480/416）、减小 `video_max_side`（960 → 640） |
| 推流更省带宽 | 降低 `jpeg_quality`（70 → 55） |
| 减少漏检 | 降低 `conf_threshold`（0.25 → 0.15，图片检测页可直接调） |

> 说明：视频数量统计为**逐帧检测统计**（未启用目标跟踪），因此「累计检出数」会大于实际
> 生物个体数；页面上同时给出「单帧峰值」与「平均每帧目标数」，这两个指标更能反映真实规模。

---

## 九、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 页面提示「未检测到模型权重文件」 | 把 `.pt` 放进 `models/`，或用 `MODEL_PATH` 指定路径，再重启 |
| 视频上传后提示「编码可能不受支持」 | 转成 MP4（H.264）编码后重试，可用格式工厂 / FFmpeg 转换 |
| 启动时提示端口被占用 | 程序会自动切换到下一个可用端口，也可用 `--port` 指定 |
| 检测结果为空 | 降低置信度阈值（图片检测页可直接改），确认权重与训练类别匹配 |
| 检测结果类别名是数字或 class_x | 权重文件未包含类别名，确认导出模型时带上了 names |
| 想清空所有历史数据 | 停止服务后删除 `data/app.db`，重启会自动重建空库 |
| pip 下载 torch 很慢 | 使用 `--index-url https://download.pytorch.org/whl/cpu`，或换国内 PyPI 镜像装其它包 |

---

## 十、扩展说明

- 目标跟踪：当前为逐帧检测统计（不跟踪）。`app/core/video_task.py` 的 `_process_frame`
  已预留接入点，后续可在此接入 tracker，把逐帧数量替换为按目标 ID 去重统计。
- 所有 SQL 均为参数化查询，文件上传做了类型与大小校验、文件名清洗，前端展示做了 HTML 转义。
