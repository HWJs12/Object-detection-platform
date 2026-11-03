"""地图站点与检测联动：使用隔离数据库和临时上传目录。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app.db import dao, database, stations
from app.main import app
from config import settings


class StationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='survey_station_test_')
        self.root=Path(self.tmp.name)
        self.previous=getattr(database._local,'conn',None)
        database._local.conn=None
        self.patches=[patch.object(settings,'db_path',self.root/'test.db'),
            patch.object(settings,'upload_dir',self.root/'uploads'),
            patch.object(settings,'result_dir',self.root/'results')]
        for p in self.patches:p.start()
        (self.root/'uploads').mkdir();(self.root/'results').mkdir()
        with patch.object(database,'_register_exit_hook'):
            database.init_db()
        self.conn=database.get_connection()
        for module in (database,dao,stations):
            p=patch.object(module,'get_connection',return_value=self.conn)
            p.start();self.patches.append(p)
        self.client=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345))
        self.image=cv2.imencode('.png',np.zeros((8,8,3),np.uint8))[1].tobytes()

    def tearDown(self):
        self.client.close()
        self.conn.close()
        for p in reversed(self.patches):p.stop()
        database._local.conn=self.previous
        self.tmp.cleanup()

    def create(self,name='月湾一号站',x=.3,y=.4,map_id=1):
        r=self.client.post('/api/stations',json={'name':name,'x':x,'y':y,'depth_m':8,'note':'礁石区','map_id':map_id})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['data']

    def update(self,s,**changes):
        body={k:s[k] for k in ('name','x','y','depth_m','note','active','version','map_id')}
        return self.client.put(f"/api/stations/{s['id']}",json={**body,**changes})

    def record(self,s=None,kind='image',total=3,maxn=0):
        data={'task_type':kind,'source_name':'test.png','created_at':'2026-10-27 12:00:00',
            'total_objects':total,'maxn_total':maxn}
        if s:data.update(site_id=s['id'],site=s['name'])
        return dao.save_detection(data,[],[(0,'海参',total)])

    def test_empty_map_crud_conflict_and_validation(self):
        self.assertEqual(self.client.get('/api/stations').json()['data']['items'],[])
        s=self.create()
        self.assertEqual(self.client.post('/api/stations',json={'name':'月湾一号站','x':.1,'y':.2}).status_code,409)
        self.assertEqual(self.client.post('/api/stations',json={'name':'  ','x':.1,'y':.2}).status_code,400)
        self.assertEqual(self.client.post('/api/stations',json={'name':'bad','x':1.1,'y':.2}).status_code,422)
        r=self.update(s,x=0,y=1,name='西礁站')
        self.assertEqual(r.status_code,200,r.text)
        new=r.json()['data'];self.assertEqual((new['x'],new['y']),(0,1))
        self.assertEqual(self.update(s,name='过期修改').status_code,409)
        self.assertEqual(self.update(new,active=False).status_code,200)
        self.assertEqual(self.client.get('/api/stations?active_only=true').json()['data']['items'],[])

    def test_rename_keeps_record_link_and_inflight_name_consistent(self):
        s=self.create();rid=self.record(s)
        s2=self.update(s,name='新站名').json()['data']
        self.assertEqual(dao.get_record(rid)['site'],'新站名')
        inflight=self.record(s)
        self.assertEqual(dao.get_record(inflight)['site'],'新站名')
        self.update(s2,active=False)
        self.record(s2)  # 已经接受的任务不因站点停用而无法完成。
        detail=self.client.get(f"/api/stations/{s['id']}").json()['data']
        self.assertEqual(detail['summary']['record_count'],3)

    def test_site_filtered_pagination_and_separate_counting(self):
        a=self.create();b=self.create('蓝渊二号')
        self.record(a);self.record(a,'video',30,5);self.record(b);self.record()
        detail=self.client.get(f"/api/stations/{a['id']}").json()['data']
        self.assertEqual(detail['summary'],{'record_count':2,'image_count':1,'video_count':1,'image_objects':3,'video_maxn_sum':5})
        data=self.client.get(f"/api/records?site_id={a['id']}&page_size=1&page=2").json()['data']
        self.assertEqual((data['total'],len(data['items'])),(2,1))
        self.assertEqual(data['items'][0]['site_id'],a['id'])
        self.assertEqual(self.client.get('/api/records').json()['data']['total'],4)

    def test_all_upload_routes_require_active_registered_station_before_upload(self):
        for route,field in [('/api/detect/image','file'),('/api/detect/images','files'),('/api/detect/video','file')]:
            for data,code in [({},422),({'site':'随便手填'},422),({'site_id':'99999'},404)]:
                r=self.client.post(route,data=data,files={field:('test.png',self.image,'image/png')})
                self.assertEqual(r.status_code,code,r.text)
        self.assertEqual(list((self.root/'uploads').iterdir()),[])
        s=self.create();self.update(s,active=False)
        r=self.client.post('/api/detect/image',data={'site_id':s['id']},files={'file':('test.png',self.image,'image/png')})
        self.assertEqual(r.status_code,409)

    def test_image_and_batch_save_site_id(self):
        s=self.create(map_id=2)
        def fake(path,name,conf,saved,survey):
            rid=dao.save_detection({'task_type':'image','source_name':name,'created_at':'2026-10-27 12:00:00',
                'total_objects':0,'saved_name':saved,**survey},[],[])
            return {'record_id':rid,'total':0,'counts':[],'detections':[]}
        with patch('app.api.detect._read_and_detect',side_effect=fake):
            for route,field in [('/api/detect/image','file'),('/api/detect/images','files')]:
                r=self.client.post(route,data={'site_id':str(s['id']),'transect':'T1','depth_m':'8'},files={field:('test.png',self.image,'image/png')})
                self.assertEqual(r.status_code,200,r.text)
        rows,_=dao.list_records(site_id=s['id'])
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['site']=='月湾一号站' and r['transect']=='T1' and r['depth_m']==8 for r in rows))

    def test_video_captures_selected_station(self):
        s=self.create(map_id=3)
        meta=SimpleNamespace(fps=10,total_frames=20,width=8,height=8,duration_sec=2)
        task=SimpleNamespace(task_id='test-job',probe=lambda:meta,parameters={'frame_skip':2,'device':'cpu'},conf_threshold=.25)
        with patch('app.api.detect.save_upload',return_value=(self.root/'test.mp4','test.mp4',10)),patch('app.api.detect.task_manager.create',return_value=task) as create,patch('app.api.detect.video_jobs.start'):
            r=self.client.post('/api/detect/video',data={'site_id':s['id']},files={'file':('test.mp4',b'fake','video/mp4')})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(create.call_args.args[3]['site_id'],s['id'])


    def test_delete_preserves_records_items_and_original_name(self):
        s=self.create();other=self.create('其他站点')
        rid=self.record(s);other_id=self.record(other)
        dao.insert_items(rid,[{'class_id':0,'class_name':'海参','confidence':.9,'bbox':[0,0,5,5]}])
        dao.insert_frame_stats(rid,[(1,.5,1,{'海参':1})])
        r=self.client.delete(f"/api/stations/{s['id']}?version={s['version']}")
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['data']['preserved_records'],1)
        record=dao.get_record(rid)
        self.assertIsNone(record['site_id'])
        self.assertEqual(record['site'],s['name'])
        self.assertEqual(dao.get_record(other_id)['site_id'],other['id'])
        for table in ('detection_items','detection_class_stats','video_frame_stats'):
            self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table} WHERE detection_id=?',(rid,)).fetchone()[0],1)
        self.assertEqual(self.client.get(f"/api/stations/{s['id']}").status_code,404)
        self.assertEqual(self.client.get('/api/stations').json()['data']['items'][0]['id'],other['id'])
        self.assertFalse(self.conn.execute('PRAGMA foreign_key_check').fetchall())

    def test_delete_requires_current_version_and_missing_station_is_not_success(self):
        s=self.create();rid=self.record(s)
        self.assertEqual(self.client.delete(f"/api/stations/{s['id']}").status_code,422)
        newer=self.update(s,name='更新后的名称').json()['data']
        self.assertEqual(self.client.delete(f"/api/stations/{s['id']}?version={s['version']}").status_code,409)
        self.assertEqual(dao.get_record(rid)['site_id'],s['id'])
        self.assertEqual(self.client.delete(f"/api/stations/{s['id']}?version={newer['version']}").status_code,200)
        self.assertEqual(self.client.delete(f"/api/stations/{s['id']}?version={newer['version']}").status_code,404)

    def test_inflight_detection_survives_deletion_and_name_reuse_does_not_relink(self):
        s=self.create()
        self.assertEqual(self.client.delete(f"/api/stations/{s['id']}?version={s['version']}").status_code,200)
        rid=self.record(s)
        self.assertIsNone(dao.get_record(rid)['site_id'])
        self.assertEqual(dao.get_record(rid)['site'],s['name'])
        r=self.client.post('/api/detect/image',data={'site_id':s['id']},files={'file':('test.png',self.image,'image/png')})
        self.assertEqual(r.status_code,404)
        replacement=self.create(s['name'])
        self.assertNotEqual(replacement['id'],s['id'])
        self.assertEqual(self.client.get(f"/api/records?site_id={replacement['id']}").json()['data']['total'],0)


    def test_map_catalog_filtering_and_counts_are_independent(self):
        catalog=self.client.get('/api/stations').json()['data']['maps']
        self.assertEqual([m['id'] for m in catalog],[1,2,3])
        self.assertEqual(len({m['image'] for m in catalog}),3)
        self.assertEqual(catalog[1]['title'],'二号海洋养殖地形图')
        sites=[self.create(f'养殖所{i}测试站',x=i/10,map_id=i) for i in (1,2,3)]
        self.record(sites[1]);self.record(sites[1],'video',7,2)
        for s in sites:
            data=self.client.get(f"/api/stations?map_id={s['map_id']}").json()['data']
            self.assertEqual([v['id'] for v in data['items']],[s['id']])
            self.assertEqual(data['items'][0]['record_count'],2 if s['map_id']==2 else 0)
        self.assertEqual(len(self.client.get('/api/stations').json()['data']['items']),3)
        self.update(sites[2],active=False)
        self.assertEqual(self.client.get('/api/stations?map_id=3&active_only=true').json()['data']['items'],[])
        self.assertEqual(len(self.client.get('/api/stations?map_id=2&active_only=true').json()['data']['items']),1)

    def test_map_validation_and_legacy_client_update(self):
        legacy=self.client.post('/api/stations',json={'name':'旧客户端站点','x':.2,'y':.3})
        self.assertEqual(legacy.status_code,200,legacy.text)
        self.assertEqual(legacy.json()['data']['map_id'],1)
        for invalid in (0,4,-1,True,'2',1.0):
            r=self.client.post('/api/stations',json={'name':'无效地图','x':.1,'y':.2,'map_id':invalid})
            self.assertEqual(r.status_code,422,r.text)
        for invalid in ('0','4','other'):
            self.assertEqual(self.client.get(f'/api/stations?map_id={invalid}').status_code,422)
        s=self.create('二号专属站',map_id=2)
        body={k:s[k] for k in ('name','x','y','depth_m','note','active','version')}
        r=self.client.put(f"/api/stations/{s['id']}",json={**body,'x':.8})
        self.assertEqual(r.status_code,200,r.text)
        updated=r.json()['data']
        self.assertEqual(updated['map_id'],2)
        self.assertEqual(self.update(updated,map_id=3).status_code,409)
        self.assertEqual(self.client.get(f"/api/stations/{s['id']}").json()['data']['map_id'],2)

    def test_old_database_migration_keeps_site_ids_and_records(self):
        import sqlite3
        conn=sqlite3.connect(str(self.root/'legacy.db'))
        conn.row_factory=sqlite3.Row
        try:
            schema=database._SCHEMA_FILE.read_text(encoding='utf-8').replace(
                '    map_id INTEGER NOT NULL DEFAULT 1 CHECK(map_id BETWEEN 1 AND 3),\n','')
            conn.executescript(schema)
            conn.execute("INSERT INTO survey_stations (name,x,y,created_at,updated_at) VALUES ('既有调查所',.4,.5,'2026-10-28','2026-10-28')")
            site_id=conn.execute('SELECT id FROM survey_stations').fetchone()[0]
            conn.execute("INSERT INTO detections (task_type,source_name,created_at,site_id,site) VALUES ('image','legacy.png','2026-10-28',?,'既有调查所')",(site_id,))
            for _ in range(2):
                database._migrate(conn)
                conn.commit()
            row=conn.execute('SELECT * FROM survey_stations').fetchone()
            self.assertEqual((row['id'],row['map_id'],row['x'],row['y']),(site_id,1,.4,.5))
            record=conn.execute('SELECT site_id,site FROM detections').fetchone()
            self.assertEqual(tuple(record),(site_id,'既有调查所'))
            self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())
        finally:
            conn.close()


if __name__=='__main__':unittest.main()
