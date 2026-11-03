"""养殖地形图目录；编号稳定，旧站点默认归入一号养殖所。"""
from app.utils.exceptions import AppError

AQUACULTURE_MAPS = (
    {'id': 1, 'name': '一号养殖所', 'title': '一号海洋养殖地形图',
     'description': '近岸海湾 · 沙滩、浅海礁石与深海坡地',
     'image': 'assets/survey-coast-map.png',
     'alt': '蓝色俯视地形图：西侧山地海岸、弯月形沙滩、浅海礁石与东侧深海'},
    {'id': 2, 'name': '二号养殖所', 'title': '二号海洋养殖地形图',
     'description': '环礁潟湖 · 弧形岛链、内海与潮汐通道',
     'image': 'assets/aquaculture-terrain-2.png',
     'alt': '蓝色俯视地形图：弧形环礁围绕中央潟湖，岛链间分布出海通道'},
    {'id': 3, 'name': '三号养殖所', 'title': '三号海洋养殖地形图',
     'description': '群岛海峡 · 双岛、曲折水道与浅海滩涂',
     'image': 'assets/aquaculture-terrain-3.png',
     'alt': '蓝色俯视地形图：南北长岛之间形成海峡，东侧分布礁石小岛'},
)

def validate_map_id(value):
    if type(value) is not int or value not in {m['id'] for m in AQUACULTURE_MAPS}:
        raise AppError('请选择一号、二号或三号养殖所', code='INVALID_MAP')
    return value

def map_catalog():
    return [dict(item) for item in AQUACULTURE_MAPS]
