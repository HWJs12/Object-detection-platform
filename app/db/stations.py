"""虚拟调查站点：稳定编号、位置、停用状态及关联统计。"""
from __future__ import annotations

import datetime as dt
import math
import sqlite3
from typing import Any

from app.db.database import get_connection, transaction
from app.utils.exceptions import AppError
from app.utils.survey import normalize_survey
from app.utils.aquaculture import validate_map_id


def get_station(station_id: int) -> dict:
    row = get_connection().execute('SELECT * FROM survey_stations WHERE id=?', (station_id,)).fetchone()
    if row is None:
        raise AppError('站点不存在，请刷新地图', code='STATION_NOT_FOUND', status_code=404)
    result = dict(row)
    result['active'] = bool(result['active'])
    return result


def list_stations(active_only: bool = False, map_id: int | None = None) -> list:
    conditions, params = [], []
    if active_only:
        conditions.append('s.active=1')
    if map_id is not None:
        conditions.append('s.map_id=?')
        params.append(validate_map_id(map_id))
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = get_connection().execute(f'''
        SELECT s.*, COUNT(d.id) AS record_count
        FROM survey_stations s LEFT JOIN detections d ON d.site_id=s.id
        {where} GROUP BY s.id ORDER BY s.map_id, s.active DESC, s.id
    ''', params).fetchall()
    return [dict(row, active=bool(row['active'])) for row in rows]


def save_station(data: dict, station_id: int | None = None) -> dict:
    name = str(data.get('name', '')).strip()
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise AppError('站点名称需为 1–64 个可显示字符', code='INVALID_STATION')
    note = str(data.get('note') or '').strip()
    if len(note) > 300:
        raise AppError('站点备注最多 300 字', code='INVALID_STATION')
    x, y = float(data['x']), float(data['y'])
    depth = data.get('depth_m')
    requested_map = data.get('map_id')
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y)):
        raise AppError('站点位置必须在地图范围内', code='INVALID_STATION')
    if depth is not None and (not math.isfinite(float(depth)) or not 0 < float(depth) <= 12000):
        raise AppError('参考水深需大于 0 且不超过 12000 米', code='INVALID_STATION')
    now = dt.datetime.now().isoformat(timespec='seconds')
    try:
        with transaction() as cur:
            if station_id is None:
                map_id = validate_map_id(1 if requested_map is None else requested_map)
                cur.execute('''INSERT INTO survey_stations
                    (name,x,y,depth_m,note,map_id,active,version,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,1,1,?,?)''', (name,x,y,depth,note,map_id,now,now))
                station_id = int(cur.lastrowid)
            else:
                current = get_station(station_id)
                if requested_map is not None and validate_map_id(requested_map) != current['map_id']:
                    raise AppError('站点所属养殖所不能通过位置编辑改变，请在目标养殖所新建站点',
                                   code='STATION_MAP_FIXED', status_code=409)
                cur.execute('''UPDATE survey_stations SET name=?,x=?,y=?,depth_m=?,note=?,
                    active=?,version=version+1,updated_at=? WHERE id=? AND version=?''',
                    (name,x,y,depth,note,int(data['active']),now,station_id,data['version']))
                if cur.rowcount != 1:
                    raise AppError('站点已在另一个页面修改，请刷新后重试', code='STATION_CHANGED', status_code=409)
                # 同步展示名称，关联始终依赖编号；已接受的后台任务仍可完成落库。
                cur.execute('UPDATE detections SET site=? WHERE site_id=?', (name,station_id))
    except sqlite3.IntegrityError as exc:
        raise AppError('该站点名称已存在，请换一个名称', code='STATION_NAME_EXISTS', status_code=409) from exc
    return get_station(station_id)


def survey_for_station(station_id: Any, transect=None, depth_m=None, survey_round=None) -> dict:
    if not isinstance(station_id, int) or isinstance(station_id, bool) or station_id < 1:
        raise AppError('请先在养殖地形图建立站点，并选择一个启用中的站点', code='STATION_REQUIRED')
    station = get_station(station_id)
    if not station['active']:
        raise AppError('所选站点已停用，请选择其他站点', code='STATION_INACTIVE', status_code=409)
    return normalize_survey({'site_id':station_id, 'site':station['name'], 'transect':transect,
        'depth_m':depth_m, 'survey_round':survey_round})


def delete_station(station_id: int, version: int) -> dict:
    """只删除站点，保留检测记录及当时的站点名称，解除位置关联。"""
    with transaction() as cur:
        station = get_station(station_id)
        if station['version'] != version:
            raise AppError('站点已在另一个页面修改，请刷新后重新确认删除',
                           code='STATION_CHANGED', status_code=409)
        cur.execute('UPDATE detections SET site_id=NULL WHERE site_id=?', (station_id,))
        preserved = cur.rowcount
        cur.execute('DELETE FROM survey_stations WHERE id=? AND version=?', (station_id, version))
        if cur.rowcount != 1:
            raise AppError('站点已发生变化，请刷新后重试', code='STATION_CHANGED', status_code=409)
    return {'id': station_id, 'preserved_records': preserved}


def station_detail(station_id: int) -> dict:
    station = get_station(station_id)
    conn = get_connection()
    summary = dict(conn.execute('''SELECT COUNT(*) AS record_count,
        COALESCE(SUM(task_type='image'),0) AS image_count,
        COALESCE(SUM(task_type='video'),0) AS video_count,
        COALESCE(SUM(CASE WHEN task_type='image' THEN total_objects ELSE 0 END),0) AS image_objects,
        COALESCE(SUM(CASE WHEN task_type='video' THEN maxn_total ELSE 0 END),0) AS video_maxn_sum
        FROM detections WHERE site_id=?''', (station_id,)).fetchone())
    station['summary'] = summary
    station['categories'] = [dict(r) for r in conn.execute('''SELECT c.class_name,
        SUM(c.object_count) AS count FROM detection_class_stats c JOIN detections d ON d.id=c.detection_id
        WHERE d.site_id=? GROUP BY c.class_name ORDER BY count DESC,c.class_name''', (station_id,))]
    return station
