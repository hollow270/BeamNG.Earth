"""Focused, fully offline Layercake -> saved snapshot -> ordinary native generator."""
from dataclasses import replace
from datetime import datetime,timezone
import asyncio
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import numpy as np

import buildingprimary as primary
import buildingoverlay as bo
import buildingoverlaystore as storemod
import buildingrender
import buildings
import blockworker as bw
import geodesy, mapcells
import layercakebuildings as lc
import netfetch
import test_buildingrender as fx

FIXTURE=json.loads((Path(__file__).parent/'fixtures/layercake_agadir.json').read_text(encoding='utf-8'))
VERSION=FIXTURE['source']


def wkb(geometry):
    if geometry['type']=='MultiPolygon':
        parts=[wkb({'type':'Polygon','coordinates':p}) for p in geometry['coordinates']]
        return struct.pack('<BII',1,6,len(parts))+b''.join(parts)
    rings=geometry['coordinates']
    return struct.pack('<BII',1,3,len(rings))+b''.join(struct.pack('<I',len(r))+b''.join(struct.pack('<dd',*p) for p in r) for r in rings)


def row(fid=1, geometry=None, **tags):
    return dict(id=fid,type='way',version=2,timestamp='2026-10-28T00:57:56Z',building='yes',
                geometry=wkb(geometry or fx.polygon(60,60)),**tags)


def source(rows=(), **kw):
    result=lc.normalize(rows,(-76.6,39.27,-76.5,39.4),VERSION)
    # Tests use the exact local interest, keeping the real bounded baseline validator.
    boxes=[bo._bbox(f.geometry) for f in result.buildings+result.parts]
    bounds=storemod._union(boxes) if boxes else bo._bbox(fx.polygon(0,0,200,200))
    bounds=(bounds[0]-1e-6,bounds[1]-1e-6,bounds[2]+1e-6,bounds[3]+1e-6)
    return replace(result,bounds=bounds,**kw)


class ParquetHTTP:
    def __init__(self,body): self.body,self.calls,self.changed=body,[],False
    def request(self,url,headers,timeout):
        self.calls.append((url,dict(headers)))
        if self.changed: return 412,{},b''
        start,end=map(int,headers['Range'][6:].split('-'))
        return 206,{'etag':VERSION['etag'],'content-range':f'bytes {start}-{min(end,len(self.body)-1)}/{len(self.body)}',
                    'last-modified':'Sun, 01 Nov 2026 14:35:59 GMT'},self.body[start:end+1]


class PrimaryTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    close=fx.RenderTest.close
    md=fx.RenderTest.md

    def persist(self,md,data,path=None,wall=1000):
        class Reader:
            def select(self,**kw): return VERSION
            def query(self,bounds): return data
        return primary.refresh(md,path or self.root/'primary',reader=Reader(),bounds=data.bounds,wall=lambda:wall)[0]

    def view(self,baseline,snapshot):
        md=self.md(baseline.originals,baseline.original_parts,street=baseline.street,frame=baseline.test_frame)
        md.pin_buildings(snapshot)
        return md

    def test_real_agadir_identity_geometry_reaches_existing_native_mesh(self):
        raw=dict(FIXTURE['row']); raw['geometry']=wkb(raw['geometry'])
        data=lc.normalize([raw],(-9.5552,30.4119,-9.5516,30.4136),VERSION)
        frame=geodesy.EnuFrame(30.4119,-9.5552,0.)
        md=self.md(frame=frame,street=True)
        snapshot=self.persist(md,data)
        view=self.view(md,snapshot)
        key=mapcells.cell_of(30.412876,-9.5524592)
        effective=view.building_cell(key).layers['buildings']
        self.assertEqual(effective.ids.tolist(),['osm:way:1572086438'])
        self.assertEqual(effective.attrs['num_floors'][0],3)
        items=buildings.tile_buildings(frame,0,0,512,512,[effective],fx.ground)
        self.assertEqual(len(items),1)
        self.assertIsNotNone(fx.mesh_fingerprint(items))
        ring,_=bo._validated(FIXTURE['row']['geometry'])
        self.assertTrue(bo._inside((-9.5524592,30.412876),ring,boundary=True))

    def test_identity_replacement_spatial_duplicate_and_independent_fallback(self):
        duplicate=replace(fx.record('ml',x=110),sources=({'dataset':'Microsoft','record_id':'ml-1'},))
        other=replace(fx.record('other',x=180),sources=({'dataset':'Microsoft','record_id':'ml-2'},))
        md=self.md([fx.record(),duplicate,other],street=True)
        data=source([row(1,fx.polygon(60,60,15)),row(2,duplicate.geometry)])
        snapshot=self.persist(md,data); view=self.view(md,snapshot)
        f=view.building_cell(fx.CELL).layers['buildings']
        self.assertEqual(f.ids.tolist(),['gers-a','ml','other'])
        self.assertEqual(len(f),3)
        records={r.feature_id:r for r in bo.from_mapgeom(f)}
        self.assertEqual(bo.geometry_digest(records['gers-a'].geometry),bo.geometry_digest(data.buildings[0].geometry))
        self.assertEqual(bo.geometry_digest(records['other'].geometry),bo.geometry_digest(other.geometry))
        for k,v in md.cell(fx.CELL,layers=('roads',)).layers['roads'].to_arrays('').items():
            np.testing.assert_array_equal(v,view.cell(fx.CELL,layers=('roads',)).layers['roads'].to_arrays('')[k])
        self.assertEqual(md.versions(),view.versions())

    def test_two_sessions_height_geometry_and_local_cache_keys(self):
        md=self.md([fx.record()]); first=self.persist(md,source([row(height='20')]))
        a=self.view(md,first)
        key=bw.building_versions(a,fx.FRAME,(0,0,256,256))
        distant=bw.building_versions(a,fx.FRAME,(3000,0,3256,256))
        second=self.persist(md,source([row(geometry=fx.polygon(60,60,18),height='30')]),wall=1700)
        b=self.view(md,second)
        self.assertNotEqual(key,bw.building_versions(b,fx.FRAME,(0,0,256,256)))
        self.assertEqual(distant,bw.building_versions(b,fx.FRAME,(3000,0,3256,256)))
        self.assertEqual(key,bw.building_versions(a,fx.FRAME,(0,0,256,256)))

    def test_cached_restart_does_not_construct_reader_or_request_freshness(self):
        md=self.md(); first=self.persist(md,source([row()]))
        with mock.patch.object(lc,'Reader',side_effect=AssertionError('startup must not fetch')):
            snapshot,report=primary.cached(md,self.root/'primary')
        self.assertEqual(snapshot.revision,first.revision); self.assertFalse(report['checked'])
        self.assertEqual(snapshot,primary.Store(self.root/'primary').load())

    def test_missing_identity_is_not_deletion_but_positive_proof_suppresses_baseline(self):
        md=self.md([fx.record()]); first=self.persist(md,source([row()]))
        absent=source([])
        unchanged=self.persist(md,absent,wall=1700)
        self.assertEqual(len(self.view(md,unchanged).building_cell(fx.CELL).layers['buildings']),1)
        proof=primary.Removal('osm:way:1',3,'2026-10-30T00:00:00Z','deleted','https://history.example.invalid/verified-fixture')
        with self.assertRaisesRegex(ValueError,'timestamp'):
            primary.prepare(md,unchanged,replace(absent,removals=(replace(proof,timestamp='2026-11-02T00:00:00Z'),)))
        deleted=self.persist(md,replace(absent,removals=(proof,)),wall=2400)
        self.assertEqual(deleted.entries[0].feature.status,'deleted')
        v=self.view(md,deleted)
        self.assertEqual(len(v.building_cell(fx.CELL).layers['buildings']),0)
        self.assertNotEqual(bw.building_versions(self.view(md,first),fx.FRAME,(0,0,256,256)),
                            bw.building_versions(v,fx.FRAME,(0,0,256,256)))
        again=self.persist(md,absent,wall=3100)
        self.assertEqual(len(self.view(md,again).building_cell(fx.CELL).layers['buildings']),0)
        with self.assertRaisesRegex(ValueError,'suppression'):
            self.persist(md,source([row()]),wall=3800)
        self.assertEqual(primary.Store(self.root/'primary').load().revision,again.revision)

    def test_parts_are_atomic_and_multipolygons_are_not_flattened(self):
        outer=fx.polygon(60,60,20,20); inner=fx.polygon(62,62,5,5)
        data=source([row(1,outer)])
        part=replace(bo.Feature('osm:way:2',inner,{'height':25},osm_ids=('osm:way:2',)),parent_id='osm:way:1')
        parent=replace(data.buildings[0],attrs={'has_parts':True},parts_complete=True)
        md=self.md(); saved=self.persist(md,replace(data,buildings=(parent,),parts=(part,)))
        effective=self.view(md,saved).building_cell(fx.CELL)
        self.assertEqual(effective.layers['building_parts'].attrs['building_id'].tolist(),['osm:way:1'])
        multiple={'type':'MultiPolygon','coordinates':[outer['coordinates'],fx.polygon(100,100)['coordinates']]}
        unsupported=source([row(3,multiple)])
        self.assertEqual(unsupported.buildings[0].geometry['type'],'MultiPolygon')
        self.assertTrue(unsupported.issues)
        with self.assertRaises(ValueError): self.persist(md,unsupported,wall=1700)
        self.assertEqual(primary.Store(self.root/'primary').load().revision,saved.revision)
        orphan=source([dict(row(4,inner),**{'building:part':'yes'})])
        self.assertIn(('osm:way:4','unknown_part_parent'),orphan.issues)

    def test_atomic_failure_corruption_cooldown_and_session_exclusion(self):
        md=self.md(); data=source([row()]); first=self.persist(md,data)
        store=primary.Store(self.root/'primary')
        with store.session(),self.assertRaises(storemod.LockTimeout):
            self.persist(md,data,wall=1700)
        with self.assertRaises(Exception): self.persist(md,data,wall=1001)
        original=primary.Store._atomic_write
        def fail_head(s,p,b):
            if p.name=='state.json': raise OSError('simulated publication crash')
            return original(s,p,b)
        with mock.patch.object(primary.Store,'_atomic_write',fail_head),self.assertRaises(OSError):
            self.persist(md,source([row(height='25')]),wall=1700)
        self.assertEqual(store.load().revision,first.revision)
        path=store.root/'state.json'; path.write_bytes(b'{broken')
        with self.assertRaises(storemod.CorruptStorage): store.load()

    def test_real_parquet_reader_uses_bounded_etag_ranges_and_geometry_containment(self):
        try: import duckdb
        except ImportError: self.skipTest('optional production DuckDB dependency unavailable')
        path=self.root/'source.parquet'
        r=dict(FIXTURE['row']); geom=r.pop('geometry'); bb=bo._bbox(geom)
        con=duckdb.connect(config={'autoinstall_known_extensions':False,'autoload_known_extensions':False})
        try:
            con.execute('''CREATE TABLE b AS SELECT ?::BIGINT AS id, 'way'::VARCHAR AS "type", 2::INTEGER AS "version",
                '2026-10-28T00:57:56Z'::TIMESTAMPTZ AS "timestamp", 'house' building, '3' "building:levels",
                'flat' "roof:shape", ?::BLOB geometry, struct_pack(xmin:=?,ymin:=?,xmax:=?,ymax:=?) bbox''',
                [r['id'],wkb(geom),*bb])
            con.execute('COPY b TO ? (FORMAT PARQUET)',[str(path)])
        finally: con.close()
        http=ParquetHTTP(path.read_bytes())
        fetcher=netfetch.Fetcher(transport=object())
        self.addCleanup(fetcher.close)
        reader=lc.Reader(self.root/'ranges',fetcher,transport=http); self.addCleanup(reader.close)
        reader.select(explicit=True)
        # Entire footprint encloses a tiny bbox: selection must not depend on vertices inside it.
        got=reader.query((-9.552455,30.412845,-9.552445,30.412855))
        self.assertEqual(got.buildings[0].feature_id,'osm:way:1572086438')
        self.assertEqual(bo.geometry_digest(got.buildings[0].geometry),bo.geometry_digest(geom))
        self.assertTrue(all(h['If-Match']==VERSION['etag'] for u,h in http.calls[1:]))
        self.assertLess(reader.ranges.used,lc.MAX_BYTES)
        with self.assertRaises(ValueError): lc.Reader(self.root/'unused',fetcher).select()

    def test_range_limits_changed_etag_and_invalid_geometry_fail_closed(self):
        single={'type':'MultiPolygon','coordinates':[fx.polygon(60,60)['coordinates']]}
        relation=dict(row(2,single),type='relation')
        self.assertEqual(source([relation]).buildings[0].feature_id,'osm:relation:2')
        self.assertFalse(source([relation]).issues)
        with self.assertRaises((ValueError,struct.error)): source([dict(row(),geometry=b'bad')])
        with self.assertRaises(ValueError): source([row(),row()])
        with self.assertRaisesRegex(ValueError,'newer'):
            source([dict(row(),timestamp='2026-11-02T00:00:00Z')])
        response=mock.Mock(status=200)
        response.getheaders.return_value=[]
        connection=mock.Mock(); connection.getresponse.return_value=response
        with mock.patch.object(lc.netfetch,'windows_ssl_context',return_value=None), \
             mock.patch.object(lc.http.client,'HTTPSConnection',return_value=connection), \
             self.assertRaises(lc.rangeread.RangeError):
            lc.RangeHTTP().request(lc.URL,{'Range':'bytes=0-15'},30)
        response.read1.assert_not_called()
        connection.close.assert_called_once()
        md=self.md(); data=source([row()]); first=self.persist(md,data)
        class Changed:
            def select(self,**kw): raise lc.rangeread.Changed('412: pinned source replaced')
        with self.assertRaises(lc.rangeread.Changed):
            primary.refresh(md,self.root/'primary',reader=Changed(),wall=lambda:1700)
        self.assertEqual(primary.Store(self.root/'primary').load().revision,first.revision)

    def test_actual_startup_pins_cached_snapshot_before_workers_without_refresh(self):
        import earthd
        md=self.md(); saved=self.persist(md,source([row()]))
        captured=[]
        class StopBeforeWorkers(Exception): pass
        def pool(*args,**kw):
            captured.append(kw['initargs'][-1])
            self.assertEqual(md._building_view.snapshot,captured[-1])
            raise StopBeforeWorkers()
        args=NS(far_format='dae',lod_k=None,near_radii=None,cache_dir=str(self.root/'primary'),offline=True,
            cache_cap=1.,surface='off',roads='on',sea='off',trees='off',buildings='on',background_duty=.5,
            detail_ring=500.,download_ahead='off',route_prefetch='off',route_prefetch_km=1.,block_process='on',
            grading='off',road_widths='off',airports='off',gas_stations='off')
        with mock.patch.dict(os.environ,{'BNGE_OSM_PRIMARY':'layercake','BNGE_OSM_STARTUP_CONFIG':'ignored-legacy-config'}), \
             mock.patch.object(lc,'Reader',side_effect=AssertionError('no freshness check')), \
             mock.patch.object(primary.buildingstartup.Config,'read',side_effect=AssertionError('legacy refresh bypassed')), \
             mock.patch.object(earthd.netfetch,'Fetcher',return_value=md.fetcher), \
             mock.patch.object(earthd.cachemod,'Cache'), \
             mock.patch.object(earthd,'mapdatamod',NS(MapData=mock.Mock(return_value=md))), \
             mock.patch.object(earthd,'prune_own_caches'), \
             mock.patch.object(earthd.plancachemod,'start_run'), \
             mock.patch.object(earthd.pacer,'DUTY',earthd.pacer.DUTY), \
             mock.patch.multiple(earthd,DETAIL_RING_M=earthd.DETAIL_RING_M,AHEAD_ON=earthd.AHEAD_ON,
                ROUTE_AHEAD_ON=earthd.ROUTE_AHEAD_ON,ROUTE_AHEAD_MAX_M=earthd.ROUTE_AHEAD_MAX_M), \
             mock.patch.object(earthd.poolsmod,'RespawningPool',side_effect=pool):
            with self.assertRaises(StopBeforeWorkers): asyncio.run(earthd.main_async(args))
        self.assertEqual(captured[0].revision,saved.revision)
        # Construction failure releases the pre-game lease, too.
        with primary.Store(self.root/'primary').session(): pass


if __name__=='__main__': unittest.main()
