# 模型权重存放目录

**仓库不包含模型权重；使用者需自行上传自己的 `.pt` 文件。**

```
models/best.pt     <- 使用者自行上传的权重文件（不会随 GitHub 仓库提供）
models/data.yaml   <- 原始训练配置(仅存档查阅, 程序运行不读取)
```

模型的类别为 `hd / hx / hs / sb / other`（程序启动后可在首页「模型与运行信息」中看到，
也可通过 `/api/model/info` 查询）。

---

如需更换权重，把新的 YOLO 权重文件（`.pt`）直接放在本目录下即可，例如：

```
models/best.pt
```

说明：

1. 程序启动时会自动扫描本目录，使用**找到的第一个 `.pt` 文件**作为检测模型。
2. 如果目录中有多个 `.pt`，或要指定其他位置，使用以下任一方式：
   - 在项目根目录创建 `config.yaml`，写入：
     ```yaml
     model_path: models/best.pt
     ```
   - 或设置**相对于项目根目录的路径**（Windows CMD）：
     ```
     set MODEL_PATH=models\best.pt
     ```
     （PowerShell 用 `$env:MODEL_PATH="models\best.pt"`）
3. 类别名称由程序从模型自身读取（`model.names`），**不需要手工填写**，也不会写死在代码里。
4. 更换权重文件后，无需重启也可以生效：调用接口 `POST /api/model/reload`，或重启服务。
