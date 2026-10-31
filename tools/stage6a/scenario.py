"""Synthetic inputs for the isolated Stage 6A development companion only.

No test-module imports, HTTP listener, public endpoint, or production-module edits.
The fixed road is baseline fixture input and is never changed by the refresh.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import buildingoverlay as bo
import buildingrender
import geodesy
import mapcells
import mapdata
import mapgeom as mg
import osmbuildings as osm
import overture

CELL=mapcells.CellKey(14,4706,6244)
LAT,LON=CELL.center()
FRAME=geodesy.EnuFrame(LAT,LON)
RELEASE='stage6a-synthetic-baseline-1'
ENDPOINT='https://stage6a-fixture.invalid/api/interpreter'
STAMP='2026-10-30T12:00:00Z'
ELEMENT_STAMP='2026-10-30T11:59:00Z'
WALL=datetime(2026,10,30,12,1,tzinfo=timezone.utc).timestamp()


def coordinate(x,y):
    la,lo,_=FRAME.enu_to_geodetic(x,y,0.)
    return float(la),float(lo)


def bounds(x0=20,y0=40,x1=250,y1=175):
    s,w=coordinate(x0,y0); n,e=coordinate(x1,y1)
    return (w,s,e,n)


def way(identity,x,y,w,d,height,version=1):
    rows=[]
    for i,(xx,yy) in enumerate(((x,y),(x+w,y),(x+w,y+d),(x,y+d))):
        la,lo=coordinate(xx,yy)
        rows.append(dict(type='node',id=identity*100+i,lat=la,lon=lo,version=version,timestamp=ELEMENT_STAMP))
    return rows+[dict(type='way',id=identity,nodes=[r['id'] for r in rows]+[rows[0]['id']],
                     version=version,timestamp=ELEMENT_STAMP,
                     tags={'building':'yes','height':str(height),'roof:shape':'flat'})]


def response(rows):
    counts={k:sum(r['type']==k for r in rows) for k in ('node','way','relation')}
    count=dict(type='count',id=0,tags=dict(nodes=str(counts['node']),ways=str(counts['way']),
               relations=str(counts['relation']),total=str(len(rows))))
    return json.dumps(dict(version=.6,generator='Stage 6A synthetic fixture, not live OSM',
        osm3s={'timestamp_osm_base':STAMP},elements=rows+[count]),sort_keys=True,separators=(',',':')).encode()


PAYLOAD=response(way(9001,60,80,24,16,24,2)+way(9002,120,80,12,16,8)+way(9003,210,80,12,16,14))


def originals():
    snapshot=osm.parse(response(way(9001,60,80,12,16,8)+way(9002,120,80,12,16,8)),bounds(),
                       now='2026-10-30T12:01:00Z')
    rows=[replace(f,feature_id='stage6a:baseline:'+str(i+1)) for i,f in enumerate(snapshot.buildings)]
    s,w=coordinate(60,130); n,e=coordinate(72,146)
    rows.append(bo.Feature('stage6a:overture-only',{'type':'Polygon','coordinates':[[[w,s],[e,s],[e,n],[w,n],[w,s]]]},
                          {'height':8,'roof_shape':'flat','class':'retail'},({'dataset':'Synthetic Overture baseline'},)))
    return tuple(rows)


def feature_arrays(records,layer):
    records=sorted(records,key=lambda f:f.feature_id)
    geoms=[(mg.POLYGON,[[np.asarray(r) for r in f.geometry['coordinates']]]) for f in records]
    f=mg.build(layer,mg.text_column([r.feature_id for r in records]),geoms)
    attrs=[buildingrender._attrs(r,layer) for r in records]
    for name in overture.LAYERS[layer].get('text',()): f.attrs[name]=mg.text_column([a.get(name) for a in attrs])
    for name in overture.LAYERS[layer].get('float',()): f.attrs[name]=mg.float_column([a.get(name) for a in attrs])
    for name in overture.LAYERS[layer].get('bool',()): f.attrs[name]=mg.bool_column([a.get(name) for a in attrs])
    for name in ('cx','cy'): f.attrs[name]=mg.float_column([a[name] for a in attrs],np.float64)
    return f


class SyntheticMapData(mapdata.MapData):
    def __init__(self,directory,*,fetcher,overlay):
        self.originals=originals()
        super().__init__(str(directory),fetcher=fetcher,release=RELEASE,building_overlays=overlay)

    def _build(self,key,layer,kind):
        if layer in mapdata.RASTER_LAYERS:
            return {'missing':np.array(True),'provenance':np.array('null')}
        if layer in ('buildings','building_parts'):
            rows=[r for r in self.originals if layer=='buildings' and buildingrender._intersects(bo._bbox(r.geometry),key.bounds())]
            f=feature_arrays(rows,layer)
        else: f=overture.empty(layer)
        if layer=='roads':
            la,lo,_=FRAME.enu_to_geodetic(np.array([-80.,280.]),np.array([20.,20.]),0.)
            f=mg.build('roads',mg.text_column(['stage6a:fixed-baseline-road']),[(mg.LINE,[[np.c_[lo,la]]])])
            f.attrs={'class':mg.text_column(['residential']),'subtype':mg.text_column(['road']),
                     'cx':np.array([lo.mean()]),'cy':np.array([la.mean()])}
            f.rules=overture.empty('roads').rules
        ox,oy=mapcells.owner(f.attrs['cx'],f.attrs['cy'],key.z)
        f.own=(ox==key.x)&(oy==key.y); f.clipped=np.zeros(len(f),bool)
        arrays=mapdata._layer_arrays(f)
        arrays['provenance']=np.array(json.dumps({'id':'stage6a:synthetic','name':'Synthetic test data','release':RELEASE,'datasets':[]} if len(f) else None))
        return arrays


class FlatDem:
    """Zero geodetic elevation; the normal Earth's-curvature conversion still runs."""
    def sample(self,lat,lon,zoom,progress=None):
        if progress: progress(1,1)
        return np.zeros(np.broadcast(np.asarray(lat),np.asarray(lon)).shape)
    def coarse_at(self,*args,**kwargs): return None
    def keys_around(self,*args,**kwargs): return ()
    def prefetch(self,*args,**kwargs): pass
    def stats_snapshot(self): return {}


class FixtureTransport:
    def __init__(self,log): self.calls=0; self.log=log
    def post(self,endpoint,body,headers,**limits):
        if endpoint!=ENDPOINT: raise RuntimeError('Stage 6A only permits its in-memory fixture')
        if len(PAYLOAD)>limits['max_bytes']: raise RuntimeError('fixture exceeds response limit')
        self.calls+=1
        self.log('fixture_request',count=self.calls,payload_sha256=hashlib.sha256(PAYLOAD).hexdigest())
        return 200,{'content-type':'application/json'},PAYLOAD


def manifest():
    return dict(synthetic=True,baseline_release=RELEASE,fixture_clock='2026-10-30T12:01:00Z',
        payload_sha256=hashlib.sha256(PAYLOAD).hexdigest(),reference_origin=dict(lat=LAT,lon=LON),
        viewpoint=dict(zip(('lat','lon'),coordinate(128,-190))),bounds=list(bounds()),
        expected_preview=dict(addition=1,replacement=1,unchanged=2,conflict=0,overture_only=1),
        targets=[dict(name='A: replacement',before=[60,80,12,16,8],after=[60,80,24,16,24]),
                 dict(name='B: unchanged',before=[120,80,12,16,8],after=[120,80,12,16,8]),
                 dict(name='C: addition',before=None,after=[210,80,12,16,14]),
                 dict(name='D: Overture-only',before=[60,130,12,16,8],after=[60,130,12,16,8])],
        dimensions='[east, north, width, depth, height] in metres from the reference origin',
        road='Fixed synthetic residential baseline: (-80,20) to (280,20); never edited by refresh.')
