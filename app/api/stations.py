"""虚拟调查站点 API。"""
from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from app.db import stations
from app.utils.response import ok
from app.utils.aquaculture import map_catalog

router = APIRouter(prefix='/api/stations', tags=['养殖地形图'])


class StationCreate(BaseModel):
    map_id: int = Field(1, ge=1, le=3, strict=True)
    name: str = Field(..., min_length=1, max_length=64)
    x: float = Field(..., ge=0, le=1)
    y: float = Field(..., ge=0, le=1)
    depth_m: Optional[float] = Field(None, gt=0, le=12000)
    note: str = Field('', max_length=300)


class StationUpdate(StationCreate):
    map_id: Optional[int] = Field(None, ge=1, le=3, strict=True)
    active: bool
    version: int = Field(..., ge=1)


@router.get('', summary='列出虚拟地图站点')
async def list_stations(active_only: bool = Query(False), map_id: Optional[int] = Query(None, ge=1, le=3)):
    return ok({'items': stations.list_stations(active_only, map_id), 'maps': map_catalog()})


@router.post('', summary='创建虚拟调查站点')
async def create_station(payload: StationCreate):
    return ok(stations.save_station(payload.model_dump()), '站点已建立')


@router.get('/{station_id}', summary='站点详情和关联统计')
async def station_detail(station_id: int):
    return ok(stations.station_detail(station_id))


@router.put('/{station_id}', summary='编辑、移动、停用或启用站点')
async def update_station(station_id: int, payload: StationUpdate):
    return ok(stations.save_station(payload.model_dump(),station_id), '站点已更新')


@router.delete('/{station_id}', summary='删除站点并保留其检测记录')
async def delete_station(station_id: int, version: int = Query(..., ge=1)):
    return ok(stations.delete_station(station_id, version), '站点已删除，检测记录已保留')
