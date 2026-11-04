"""Ordinary building-generator adapter containing OSM data only, including empty areas."""
from copy import deepcopy
from dataclasses import replace
import numpy as np

import buildingoverlay as geom
import buildingoverlaystore as fs
from buildingrender import Dependency,_hash,_render_identity
import mapgeom as mg
import mapcells
from osmbuildingcache import MODE,attributes

NUMBERS={'height','num_floors','num_floors_underground','min_height','min_floor','roof_height','roof_direction','cx','cy'}


class View:
    active=True  # even empty/unavailable must exclude the baseline and version old meshes
    cache_bytes=0

    def __init__(self,snapshot,report=None):
        if snapshot.baseline_release!=MODE:raise ValueError('OSM-only snapshot required')
        for entry in snapshot.entries:
            if entry.baseline_digest is not None:raise ValueError('matched baseline entries are not OSM-only')
            for feature in (entry.feature,)+entry.parts:
                if geom.normalize_osm_id(feature.feature_id)!=feature.feature_id or feature.status!='building' or not feature.sources or \
                        any(s.get('dataset')!='OpenStreetMap' for s in feature.sources) or \
                        any(s.get('dataset')!='OpenStreetMap' for values in feature.attribute_sources.values() for s in values):
                    raise ValueError('non-OSM render input rejected')
                attributes(feature)
        self.snapshot=deepcopy(snapshot);self.report=report or {}

    def dependency(self,keys,*,render_bounds=None):
        entries=[e for e in self.snapshot.entries if any(fs._intersects(e.bounds,k.bounds()) for k in keys)
                 and (render_bounds is None or fs._intersects(e.bounds,render_bounds))]
        full=[[e.feature.feature_id,geom.MergeResult((_render_identity(e.feature),),tuple(_render_identity(p) for p in e.parts),()).digest()]
              for e in sorted(entries,key=lambda e:e.feature.feature_id)]
        geo=[[e.feature.feature_id,geom.geometry_digest(e.feature.geometry),
              [(p.feature_id,geom.geometry_digest(p.geometry)) for p in e.parts]] for e in sorted(entries,key=lambda e:e.feature.feature_id)]
        return Dependency(_hash([MODE,full]),_hash([MODE,geo]),tuple(e.feature.feature_id for e in sorted(entries,key=lambda e:e.feature.feature_id)))

    def cell(self,md,key,layers,kind):
        # Independent road/water/etc inputs keep their original contract. Never
        # request a baseline building layer, even when this view is empty.
        base=md.cell(key,layers=tuple(l for l in layers if l not in ('buildings','building_parts')),kind=kind)
        token=self.dependency((key,)).revision;out=dict(base.layers)
        entries=[e for e in self.snapshot.entries if fs._intersects(e.bounds,key.bounds())]
        for layer in ('buildings','building_parts'):
            if layer not in layers:continue
            records=sorted([f for e in entries for f in ((e.feature,) if layer=='buildings' else e.parts)],key=lambda f:f.feature_id)
            f=mg.build(layer,mg.text_column([r.feature_id for r in records]),
                       [(mg.POLYGON,[[np.asarray(r,np.float64) for r in record.geometry['coordinates']]]) for record in records])
            attrs=[]
            for record in records:
                a=deepcopy(record.attrs);box=geom._bbox(record.geometry)
                a.update(cx=(box[0]+box[2])/2,cy=(box[1]+box[3])/2,src_dataset='OpenStreetMap',
                         src_record=record.sources[0]['record_id'],height_src='OpenStreetMap',sources=geom._json(record.sources))
                if record.parent_id is not None:a['building_id']=record.parent_id
                attrs.append(a)
            columns=NUMBERS|{'has_parts','class','subtype','roof_shape','roof_orientation','roof_material','facade_material',
                            'facade_color','roof_color','building_id','src_dataset','src_record','height_src','sources'}
            columns|=set().union(*(set(a) for a in attrs))
            for column in columns:
                values=[a.get(column) for a in attrs]
                f.attrs[column]=mg.float_column(values,np.float64 if column in ('cx','cy') else np.float32) if column in NUMBERS else mg.bool_column(values) if column=='has_parts' else mg.text_column(values)
            ox,oy=mapcells.owner(f.attrs['cx'],f.attrs['cy'],key.z)
            f.own=(ox==key.x)&(oy==key.y);f.clipped=np.zeros(len(f),bool);f.localize(base.frame)
            f.__dict__['_bnge_building_dependency']=token
            f.__dict__['_bnge_osm_only']=True
            f.__dict__['overlay_provenance']={r.feature_id:dict(sources=deepcopy(r.sources),parent_id=r.parent_id) for r in records}
            out[layer]=f
            base.stats[layer]=dict(s=0.0,hit=True)
        provenance=dict(id='osm-buildings',name='OpenStreetMap buildings',licence='ODbL-1.0',
                        attribution='© OpenStreetMap contributors',mode=MODE,revision=token)
        return replace(base,layers=out,versions=dict(base.versions,building_overlay=token),provenance=base.provenance+[provenance])
