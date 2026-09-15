# EL Cell Locator — 光伏组件 EL 暗斑电池片定位服务

产线 EL（电致发光）相机按安装方向不同会输出旋转 0°/90°/180°/270° 的图像。
本服务把暗斑像素坐标先归一化到正立画布，再换算成从 1 开始的电池片
`(row, col)`，避免返修人员因图像方向不一致而拆错位置。

无数据库、无前端：一个 FastAPI 进程，所有坐标与行列均在请求时真实计算。

## 坐标约定

- 原点位于图像**左上角**，`x` 向右、`y` 向下。
- 输入点必须满足 `0 ≤ x < width`、`0 ≤ y < height`（均为整数）。
- 顺时针归一化规则（归一化坐标记为 `(u, v)`）：

| rotation | 归一化公式 `(u, v)` | 归一化画布宽 × 高 |
|---|---|---|
| 0 | `(x, y)` | `width × height` |
| 90 | `(height - 1 - y, x)` | `height × width`（宽高互换） |
| 180 | `(width - 1 - x, height - 1 - y)` | `width × height` |
| 270 | `(y, width - 1 - x)` | `height × width`（宽高互换） |

- 电池片编号（从 1 开始）：
  `row = floor(v × rows / 归一化高度) + 1`，`col = floor(u × cols / 归一化宽度) + 1`。
- 恰好落在分界线上的像素归入**右侧 / 下侧**电池片（floor 语义自然保证）。

## API

### `POST /inspect`

请求体：

```json
{
  "width": 600,
  "height": 400,
  "rows": 4,
  "cols": 10,
  "rotation": 90,
  "points": [{"x": 0, "y": 0}, {"x": 599, "y": 399}]
}
```

- `width` / `height` / `rows` / `cols`：正整数（严格校验，浮点、字符串、布尔一律 422）。
- `rotation`：仅允许 `0`、`90`、`180`、`270`。
- `points`：非空整数点数组。

响应 `200`（保持输入顺序，`x`/`y` 为归一化坐标）：

```json
{
  "rotation": 90,
  "canvas": {"width": 400, "height": 600},
  "grid": {"rows": 4, "cols": 10},
  "results": [
    {"index": 0, "x": 399, "y": 0, "row": 1, "col": 10},
    {"index": 1, "x": 0, "y": 599, "row": 4, "col": 1}
  ]
}
```

任一项非法时**整批**返回 `422`，`detail` 中带出错点的数组下标 `index`，
且响应中不含任何部分计算结果：

```json
{
  "detail": [
    {
      "type": "point_out_of_bounds",
      "loc": ["body"],
      "msg": "points[1] = (600, 3) violates 0 <= x < 600 and 0 <= y < 400",
      "index": 1
    }
  ]
}
```

### `POST /trace`

返修工程师按顺序标注一串暗斑中心表示一条裂纹，本接口返回裂纹**依次进入**
的电池片行列路径。请求体与 `/inspect` 相同，仅把非空 `points` 换成非空
`vertices`（同一画布、网格、方向，顶点为有序折线）：

```json
{
  "width": 600,
  "height": 400,
  "rows": 4,
  "cols": 6,
  "rotation": 0,
  "vertices": [{"x": 50, "y": 50}, {"x": 150, "y": 150}]
}
```

响应 `200`，含归一化后的 `vertices`（保持输入顺序，含 `index`）与有序
`path`（每个元素为 `{row, col}`，1 基）：

```json
{
  "rotation": 0,
  "canvas": {"width": 600, "height": 400},
  "grid": {"rows": 4, "cols": 6},
  "vertices": [
    {"index": 0, "x": 50, "y": 50},
    {"index": 1, "x": 150, "y": 150}
  ],
  "path": [{"row": 1, "col": 1}, {"row": 2, "col": 2}]
}
```

路径计算与归属规则：

- 每个顶点先用与 `/inspect` 相同的旋转公式逐点归一化；再把折线每一段与
  非均匀网格线求交。相交参数是精确有理数，**只用整数交叉乘法比较大小**，
  网格非整除（分界线落在小数像素）时也不依赖任何浮点舍入。
- 单元区域按**左上闭、右下开**解释，画布最右、最下边缘闭合；线段恰好沿
  分界线行走时归**右侧 / 下侧**单元。
- 线段在同一参数处同时穿过一条横向、一条纵向分界线（格网角点）时，直接
  进入**对角单元**，不会记录只碰到该角点的两个旁侧单元。
- 连续段交界产生的相同行列只保留一次；离开后再次进入的**非连续重访保留**；
  重复顶点按零长度线段处理，不产生额外记录；单点折线正常返回其所在单元。

校验与 `/inspect` 一致且为整批语义：任一顶点越界或类型非法（浮点、字符串、
布尔、缺坐标、非对象元素等）都返回 `422`；**多个顶点同时非法时，按输入
位置返回全部错误**，每个错误带对应 `index`，响应体不含任何部分结果：

```json
{
  "detail": [
    {"type": "vertex_out_of_bounds", "loc": ["body", "vertices", 0], "msg": "...", "index": 0},
    {"type": "int_type", "loc": ["body", "vertices", 1, "x"], "msg": "...", "index": 1}
  ]
}
```

另有 `GET /health` 返回 `{"status": "ok"}`，供健康检查使用。

## 坐标示例（width=600, height=400, rows=4, cols=10）

0°/180° 画布为 600×400（每片 60×100 像素），90°/270° 画布为 400×600
（每片 40×150 像素）：

| rotation | 输入点 (x, y) | 归一化 (x, y) | row | col | 说明 |
|---|---|---|---|---|---|
| 0 | (0, 0) | (0, 0) | 1 | 1 | 左上角 |
| 0 | (60, 100) | (60, 100) | 2 | 2 | 分界线像素归入右/下侧 |
| 0 | (599, 399) | (599, 399) | 4 | 10 | 右下角 |
| 90 | (0, 0) | (399, 0) | 1 | 10 | 原左上 → 右上 |
| 90 | (599, 399) | (0, 599) | 4 | 1 | 原右下 → 左下 |
| 90 | (150, 359) | (40, 150) | 2 | 2 | 归一化后恰在分界线上 |
| 180 | (0, 0) | (599, 399) | 4 | 10 | 对角翻转 |
| 180 | (599, 399) | (0, 0) | 1 | 1 | |
| 270 | (0, 0) | (0, 599) | 4 | 1 | 原左上 → 左下 |
| 270 | (599, 399) | (399, 0) | 1 | 10 | |
| 270 | (449, 40) | (40, 150) | 2 | 2 | 归一化后恰在分界线上 |

curl 示例：

```bash
curl -s -X POST http://localhost:8000/inspect \
  -H 'Content-Type: application/json' \
  -d '{"width":600,"height":400,"rows":4,"cols":10,"rotation":270,
       "points":[{"x":449,"y":40},{"x":0,"y":0}]}'
```

返回：

```json
{
  "rotation": 270,
  "canvas": {"width": 400, "height": 600},
  "grid": {"rows": 4, "cols": 10},
  "results": [
    {"index": 0, "x": 40, "y": 150, "row": 2, "col": 2},
    {"index": 1, "x": 0, "y": 599, "row": 4, "col": 1}
  ]
}
```

## 本地运行与测试

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # 服务监听 http://localhost:8000
pytest                                 # /inspect 建表测试 + /trace 参考实现/四方向对拍
python verify.py                       # 对本地已启动的服务做一次性验收
```

## Docker Compose

```bash
docker compose up api                              # 默认宿主端口 8000
API_PORT=9000 docker compose up api                # 宿主端口由 API_PORT 覆盖
docker compose up --abort-on-container-exit verify # 一次性验收：verify 跑完即退出
echo $?                                            # 0 = 验收通过
```

`verify` 服务等待 `api` 健康后，对四种旋转方向、分界线归属、顺序保持、
整批拒绝以及 `/trace` 的角点穿越、沿线行走、折返重访与四方向等价路径做
真实 HTTP 校验，全部通过则以退出码 0 结束，否则为 1。

## 目录结构

```
app/
  main.py        # FastAPI 入口、422 异常处理（携带数组下标）、/inspect 与 /trace
  models.py      # 严格校验的请求/响应模型（整批边界校验、vertices 全量聚合）
  transform.py   # 纯整数几何：归一化 + 电池片定位 + 折线跨格追踪
tests/
  test_inspect.py# /inspect pytest 建表测试
  test_trace.py  # /trace pytest：独立 Fraction 参考实现 × 四方向随机对拍
verify.py        # 一次性验收脚本（compose 中的 verify 服务）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
