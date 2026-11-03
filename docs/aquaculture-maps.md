# 海洋养殖地形图素材说明

页面：`survey_map.html`。保留原有一号地图，新增二号与三号地图，三张均为虚拟地形示意。

## 项目中的素材

- 一号：`web/assets/survey-coast-map.png`，原有近岸海湾。
- 二号：`web/assets/aquaculture-terrain-2.png`，环礁、潟湖与潮汐通道。
- 三号：`web/assets/aquaculture-terrain-3.png`，双岛、海峡与浅海滩涂。

两张新图均使用内置 imagegen 生成，以原有地图为画风参考。所有名称由前端显示，图片内不包含文字、站点或建筑。

## 生成提示词

二号：Match the supplied reference's blue monochrome 2D overhead hand-painted game-map style, slate-blue land, cyan shallow-water shelves and navy deep ocean. Create a completely new geography: a broken oval coral atoll around a large turquoise inner lagoon, a rugged crescent island along the lower-left side, small rocky islets in an upper-right arc, and two wide sea channels opening at upper-left and lower-right. True top-down plan with illustrated relief, wide landscape about 1.6:1. Empty natural terrain only: no buildings, boats, pens, people, labels, text, numbers, markers, logos, watermarks or UI. Keep open water areas for draggable user station markers.

三号：Match the supplied reference's blue 2D overhead illustrated game-map style, fine rocky topography, steel-blue land, cyan coastal shallows and midnight-navy deep sea. Create different geography: an elongated mountainous island across the upper half, a smaller long rocky island across the lower half, a winding deep-water strait between them, branching shallow tidal inlets, irregular beaches and scattered reef islets towards the right edge. True near-vertical 2D plan with hand-painted relief, landscape about 1.6:1. Not an oval atoll or the original left-side bay. Only natural island geography and ocean bathymetry: no text, numbers, labels, buildings, stations, boats, people, pens, markers, logos, watermarks, borders or UI.

## 站点与检测联动

站点增加 `map_id` 字段，取值 1、2、3。旧库增量迁移时默认归入一号养殖所，站点编号、坐标及原有检测记录保持关联。地图页面按所属养殖所查询站点；图片、视频检测页先选养殖所，再选其中的启用站点。更换养殖所会清空旧站点，并同步网址参数，刷新后保留正确选择。

站点名称仍沿用原系统的全局唯一规则。编辑位置不改变站点所属养殖所。删除确认弹框及保留历史检测记录的行为沿用现有逻辑。

## 验证

139 项 Python 回归测试通过，包含新增的地图隔离、地图参数校验、兼容旧客户端与旧数据库迁移测试；既有前端 contracts 检查通过。独立验收库中检查了三张地图切换、拖动与位置持久化、新建编辑停用、删除取消与确认、图片与视频检测选站点，以及明暗主题显示。正式库启动后核对原有 239 条检测记录和明细不变。
