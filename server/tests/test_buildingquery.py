"""Real local Parquet/fsspec/DuckDB execution; no source service required."""
from dataclasses import replace
import math
from unittest import mock
import unittest

import buildingoverlay as bo
import buildingregions as regions
import buildingspatial
import layercakebuildings as lc
import netfetch
import test_buildingprimary as fixture
import test_buildingrender as fx


class QueryTest(unittest.TestCase):
    setUp=fx.RenderTest.setUp
    close=fx.RenderTest.close

    def reader(self,count=1):
        import duckdb
        path=self.root/'fixture.parquet'
        con=duckdb.connect(config={'autoinstall_known_extensions':False,'autoload_known_extensions':False})
        geometry=fixture.FIXTURE['row']['geometry'];bounds=bo._bbox(geometry)
        try:
            con.execute('''CREATE TABLE b AS SELECT i+1 AS id, 'way' AS type, 2::INTEGER AS version,
                '2026-10-28T00:57:56Z' AS timestamp, 'house' building, ?::BLOB geometry,
                struct_pack(xmin:=?,ymin:=?,xmax:=?,ymax:=?) bbox FROM range(?) t(i)''',
                [fixture.wkb(geometry),*bounds,count])
            con.execute('COPY b TO ? (FORMAT PARQUET)',[str(path)])
        finally:con.close()
        http=fixture.ParquetHTTP(path.read_bytes());fetcher=netfetch.Fetcher(transport=object())
        self.addCleanup(fetcher.close)
        reader=lc.Reader(self.root/'ranges',fetcher,transport=http);self.addCleanup(reader.close)
        reader.select(explicit=True)
        return reader,http,(bounds[0]-.001,bounds[1]-.001,bounds[2]+.001,bounds[3]+.001)

    def test_staged_and_original_query_identical_then_no_remote_rescan(self):
        reader,http,bounds=self.reader()
        before=reader.query(bounds)
        reader.prepare_region(bounds);count=len(http.calls)
        for _ in range(3):self.assertEqual(reader.query(bounds),before)
        self.assertEqual(len(http.calls),count)
        self.assertEqual(reader.diagnostics['remote_scans'],2)  # one original + one staging scan
        self.assertEqual(reader.diagnostics['local_queries'],3)
        with self.assertRaisesRegex(ValueError,'outside'):reader.query((bounds[0]-.001,*bounds[1:]))

    def test_proven_empty_staging_and_dense_partition_limits(self):
        reader,http,bounds=self.reader(513)
        reader.prepare_region(bounds)
        with self.assertRaisesRegex(lc.ResourceLimit,'partition feature budget'):reader.query(bounds)
        count=len(http.calls)
        empty=(bounds[0],bounds[1],bounds[0]+.00001,bounds[1]+.00001)
        self.assertEqual(reader.query(empty).buildings,())
        self.assertEqual(len(http.calls),count)

    def test_no_candidate_groups_is_complete_empty_without_invalid_sql(self):
        reader,http,bounds=self.reader()
        with mock.patch.object(reader,'_candidates',return_value=[]):reader.prepare_region(bounds)
        result=reader.query(bounds)
        self.assertTrue(result.complete);self.assertEqual(result.buildings,())
        self.assertEqual(reader.diagnostics['remote_scans'],0)

    def test_source_staging_bounds_and_row_capacity_remain_bounded(self):
        reader,http,bounds=self.reader(50001)
        with self.assertRaisesRegex(lc.ResourceLimit,'staging feature capacity'):reader.prepare_region(bounds)
        with self.assertRaises(lc.ResourceLimit):reader.prepare_region((-1,-1,1,1))

    def test_duplicate_prefilter_preserves_both_projection_paths(self):
        for lat in (0,30,70,84):
            sx=111320*math.cos(math.radians(lat))
            a=tuple((x/sx,lat+y/110540) for x,y in ((0,0),(10,0),(10,10),(0,10)))
            for dx in (0,.01,.1,.2,1,50):
                b=tuple((x+dx/sx,y) for x,y in a)
                aa,bb=bo._xy(a,a[0]),bo._xy(b,a[0])
                original=any(all(math.dist(p,q)<=bo.DUPLICATE_TOLERANCE_M for p,q in zip(aa,bb[k:]+bb[:k])) for k in range(len(b))) or buildingspatial.equivalent(a,b,bo.DUPLICATE_TOLERANCE_M)
                self.assertEqual(bo._equivalent(a,b),original,(lat,dx))
        with mock.patch.object(buildingspatial,'equivalent',side_effect=AssertionError('distant exact clipping')):
            self.assertFalse(bo._equivalent(a,tuple((x+1,y) for x,y in a)))

    def test_grouping_checks_each_pair_once_across_multiple_buckets(self):
        a=fixture.source([fixture.row()]).buildings[0]
        b=replace(a,feature_id='osm:way:2',osm_ids=('osm:way:2',))
        with mock.patch.object(bo,'_overlap',wraps=bo._overlap) as overlap:
            groups=regions._groups((a,b))
            self.assertEqual(len(groups),1);self.assertEqual(overlap.call_count,1)


if __name__=='__main__':unittest.main()
