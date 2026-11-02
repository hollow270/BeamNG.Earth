"""Explicit, ETag-pinned Layercake building reads through the project's Parquet FS.

No startup checks, DuckDB HTTP extension, endpoint rotation, or deletion inference.
Only complete bounded neighbourhoods are returned. Unsupported geometry is retained
as a diagnostic, never flattened into a different footprint.
"""
from dataclasses import dataclass
from datetime import timezone
import email.utils
import hashlib
import http.client
import math
from pathlib import Path
import re
import time
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import buildingoverlay as bo
from buildingoverlaystore import ResourceLimit, _bounds
from buildingoverlaystore import OverlayStore
import mapgeom
import netfetch
import osmbuildings as osm
import osmtags
import rangeread

URL = 'https://data.openstreetmap.us/layercake/buildings.parquet'
PATH = 'osm/buildings.parquet'
MAX_BYTES = 96 * 1024 * 1024
MAX_FEATURES = 512
MAX_FOOTER = 32 * 1024 * 1024
TAGS = ('building', 'building:part', 'height', 'building:levels', 'min_height',
        'building:min_level', 'roof:height', 'roof:shape', 'roof:orientation',
        'roof:direction', 'roof:material', 'roof:colour', 'roof:color',
        'building:material', 'building:colour', 'building:color', 'name')


class RangeHTTP:
    """Bound GET bodies *while reading*, even if a server ignores Range."""
    def request(self, url, headers, timeout):
        match = re.fullmatch(r'bytes=(\d+)-(\d+)', headers.get('Range', ''))
        if url != URL or match is None:
            raise ValueError('only bounded Layercake building ranges are permitted')
        limit = int(match[2]) - int(match[1]) + 1
        if not 0 < limit <= 16 * 1024 * 1024:
            raise ResourceLimit('range exceeds read budget')
        u = urlsplit(url)
        context = netfetch.windows_ssl_context()
        kwargs = {'timeout': min(timeout, 30)}
        if context is not None: kwargs['context'] = context
        conn = http.client.HTTPSConnection(u.hostname, **kwargs)
        response = None
        deadline = time.monotonic() + 45
        try:
            conn.request('GET', u.path, headers=dict(headers, **{'Accept-Encoding': 'identity'}))
            response = conn.getresponse()
            rh = {k.lower(): v for k,v in response.getheaders()}
            if response.status != 206:
                if response.status == 200: raise rangeread.RangeError('source ignored bounded Range')
                return response.status, rh, b''
            if rh.get('content-encoding', 'identity') != 'identity':
                raise rangeread.RangeError('unexpected range compression')
            if 'content-length' in rh and (not rh['content-length'].isdigit() or int(rh['content-length']) > limit):
                raise rangeread.RangeError('oversized range response')
            out = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('Layercake range deadline')
                if conn.sock: conn.sock.settimeout(min(timeout, remaining))
                chunk = response.read1(min(65536, limit + 1 - len(out)))
                if time.monotonic()>deadline: raise TimeoutError('Layercake range deadline')
                if not chunk: break
                out.extend(chunk)
                if len(out) > limit: raise ResourceLimit('oversized range response')
            return response.status, rh, bytes(out)
        finally:
            if response is not None: response.close()
            conn.close()


class RangeFetcher:
    """Share netfetch admission/policies/breakers, but never its unbounded GET body."""
    def __init__(self, fetcher, transport=None):
        self.fetcher, self.transport = fetcher, transport or RangeHTTP()
        self.used = 0
        self.lock = threading.Lock()

    def get(self, url, *, kind='demand', headers=None):
        m = re.fullmatch(r'bytes=(\d+)-(\d+)', (headers or {}).get('Range', ''))
        amount = int(m[2])-int(m[1])+1 if m else MAX_BYTES+1
        with self.lock:
            if self.used + amount > MAX_BYTES: raise ResourceLimit('Layercake refresh network budget')
            self.used += amount  # reserve before the request; failures count too
        with self.fetcher.session(url, kind=kind) as session:
            status, rh, body = self.transport.request(url, dict(session.headers, **(headers or {})),
                                                    self.fetcher.policy(urlsplit(url).hostname).timeout)
            session.status, session.nbytes = status, len(body)
            if status != 206: raise osm.HTTPFailure(status, rh)
            if len(body) > amount: raise ResourceLimit('oversized range transport response')
            if (headers or {}).get('If-Match') and rh.get('etag')!=(headers or {})['If-Match']:
                raise rangeread.Changed('range response did not prove its pinned ETag')
            return SimpleNamespace(status=status, headers=rh, body=body)

    def miss_alive(self, *args): return False


@dataclass(frozen=True)
class Snapshot:
    bounds: tuple
    buildings: tuple
    parts: tuple
    version: dict
    issues: tuple = ()
    complete: bool = True
    removals: tuple = ()  # only a separate authoritative identity-state source can supply these


class Reader(osmtags.OsmTags):
    """Reuse row-group planning, range cache and DuckDB configuration from osmtags."""
    def __init__(self, raw_dir, fetcher, *, transport=None):
        self.ranges = RangeFetcher(fetcher, transport)
        super().__init__(str(raw_dir), self.ranges, URL)
        self.selected = None

    def close(self): self._reader.close()

    def select(self, *, explicit=False):
        if explicit is not True: raise ValueError('source selection requires an explicit refresh request')
        if self.selected is not None: raise ValueError('one immutable source version per reader')
        # Conservative admission: no automatic pruning of retained source bytes.
        # Reserve a whole bounded operation before a new version can write blocks.
        root=Path(self.raw_dir)/'osm-buildings'
        for p in (root,*root.absolute().parents): OverlayStore._no_link(p)
        size=count=0
        if root.exists():
            for p in root.rglob('*'):
                OverlayStore._no_link(p)
                count+=1
                if count>4096: raise ResourceLimit('raw building cache entry limit')
                if p.is_file(): size+=p.stat().st_size
                if size+MAX_BYTES>256*1024*1024: raise ResourceLimit('raw building cache capacity; no automatic cleanup')
        response = self.ranges.get(URL, headers={'Range': 'bytes=0-15'})
        etag = response.headers.get('etag', '').strip()
        match = rangeread.CONTENT_RANGE.fullmatch(response.headers.get('content-range', ''))
        if not re.fullmatch(r'"[!#-~]{1,200}"', etag) or not match or match[3] == '*' or \
                int(match[1]) != 0 or int(match[2]) != 15 or len(response.body) != 16 or response.body[:4] != b'PAR1':
            raise rangeread.RangeError('missing strong ETag, Parquet magic or exact range size')
        stamp = email.utils.parsedate_to_datetime(response.headers['last-modified'])
        if stamp.tzinfo is None: raise ValueError('source timestamp lacks timezone')
        self.selected = dict(etag=etag, size=int(match[3]),
                             last_modified=stamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z'), url=URL)
        directory = Path(self.raw_dir)/'osm-buildings'/hashlib.sha256(etag.encode()).hexdigest()
        self._remote = (etag, rangeread.RemoteFile(URL, int(match[3]), str(directory), self.ranges, if_match=etag))
        return dict(self.selected)

    def _groups(self, con, uri, remote, kind):
        tail = remote.read(max(0,remote.size-8), remote.size, kind)
        if len(tail) != 8 or tail[-4:] != b'PAR1' or not 0 < int.from_bytes(tail[:4],'little') <= min(MAX_FOOTER,remote.size-8):
            raise rangeread.RangeError('invalid/oversized Parquet footer')
        return super()._groups(con,uri,remote,kind)

    def query(self, bounds):
        bounds = _bounds(bounds)
        if self.selected is None: raise ValueError('select a source version explicitly first')
        if max((bounds[2]-bounds[0])*111320*math.cos(math.radians((bounds[1]+bounds[3])/2)),
               (bounds[3]-bounds[1])*111320) > 2000:
            raise ResourceLimit('developer refresh requires bounded internal partitions')
        remote = self._remote[1]
        con, proto, errors = self._reader._connect({PATH:remote}, 'demand')
        try:
            con.execute("SET memory_limit='256MB'")
            con.execute("SET temp_directory=''")
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con,uri,remote,'demand')
            schema = {r[0] for r in con.execute(f'DESCRIBE SELECT * FROM read_parquet({uri})').fetchall()}
            if not {'id','geometry','bbox','building','version','timestamp'} <= schema:
                raise ValueError('unsupported Layercake building schema')
            self.schema = tuple(sorted(schema))
            candidates = self._candidates(groups,bounds)
            if not candidates: return Snapshot(bounds,(),(),dict(self.selected))
            wanted = {'id','geometry','bbox','version','timestamp','osm_type','type','building_id','parent_id'} | set(TAGS)
            self._reader._fetch({PATH:remote},{PATH:[r for rg in candidates for col,rs in groups[rg]['cols'].items()
                                                   if col in wanted for r in rs]},'demand')
            only = ' OR '.join(f"file_row_number BETWEEN {groups[rg]['first']} AND {groups[rg]['first']+groups[rg]['n']-1}"
                               for rg in candidates)
            w,s,e,n = bounds
            box = f'bbox.xmin <= {e!r} AND bbox.xmax >= {w!r} AND bbox.ymin <= {n!r} AND bbox.ymax >= {s!r}'
            columns = sorted((wanted & schema)-{'geometry','bbox'})
            # Core WKB conversion; no extension install, HTTPFS or DuckDB sockets.
            try: con.execute('SET enable_geoparquet_conversion=false')
            except Exception: pass
            expressions=[f'CAST("{c}" AS VARCHAR) AS "{c}"' if c=='timestamp' else f'"{c}"' for c in columns]
            rows = con.execute(f'''SELECT {','.join(expressions)}, geometry
                FROM read_parquet({uri}, file_row_number=true) WHERE ({only}) AND {box}
                LIMIT {MAX_FEATURES+1}''').fetchall()
            if len(rows)>MAX_FEATURES: raise ResourceLimit('building partition feature budget exceeded')
            return normalize([dict(zip(columns+['geometry'],r)) for r in rows],bounds,self.selected)
        except Exception:
            if errors: raise errors[0]
            raise
        finally: con.close()


def normalize(rows, bounds, version):
    """Normalize the actual flattened Layercake schema; retain full WKB topology."""
    buildings, parts, issues, identities = {}, {}, [], set()
    for row in rows:
        identity = bo.normalize_osm_id(row.get('id'))
        if identity is None and row.get('osm_type',row.get('type')) in ('way','relation'):
            identity = bo.normalize_osm_id(str(row.get('osm_type',row.get('type')))+'/'+str(row['id']))
        if identity is None: raise ValueError('missing typed OSM building identity')
        if identity in identities: raise ValueError('duplicate OSM identity in complete source')
        identities.add(identity)
        raw = bytes(row['geometry'])
        if len(raw)>128*1024: raise ResourceLimit('building geometry byte limit')
        # _geom reports the consumed byte count too: reject trailing/truncated data.
        kind, polygons, end = mapgeom._geom(raw,0)
        if end != len(raw) or kind != mapgeom.POLYGON: raise ValueError('invalid polygon WKB')
        coords = [[ring.tolist() for ring in poly] for poly in polygons]
        geometry = {'type':'Polygon','coordinates':coords[0]} if len(coords)==1 else {'type':'MultiPolygon','coordinates':coords}
        error = bo._validated(geometry)[1]
        if error: issues.append((identity,error))
        tags = {k:str(row[k]) for k in TAGS if row.get(k) is not None}
        attrs = osm._attributes({'tags':tags},issues_local:=[],identity)
        issues.extend((i.feature_id,i.code) for i in issues_local)
        is_part = tags.get('building:part','no') not in ('no','', 'false','0')
        if not is_part and tags.get('building','no') in ('no','', 'false','0'):
            issues.append((identity,'non_building_record_requires_identity_state_verification')); continue
        parent = bo.normalize_osm_id(row.get('building_id',row.get('parent_id')))
        if is_part and parent is None: issues.append((identity,'unknown_part_parent'))
        edit = str(row.get('timestamp') or '')
        if hasattr(row['timestamp'],'isoformat'): edit=row['timestamp'].isoformat()
        edit=osm._stamp(osm._time(edit))
        if osm._time(edit)>osm._time(version['last_modified']): raise ValueError('element newer than published source')
        number=row.get('version')
        if type(number) is not int or number<=0: raise ValueError('missing OSM element version')
        letter='w' if ':way:' in identity else 'r'
        source=dict(dataset='OpenStreetMap',record_id=f'{letter}{identity.rsplit(":",1)[1]}@{number}',
                    update_time=edit,dataset_etag=version['etag'],dataset_timestamp=version['last_modified'],
                    licence='ODbL-1.0',url=URL,tags=tags)
        feature=bo.Feature(identity,geometry,attrs,(source,),osm_ids=(identity,),parent_id=parent if is_part else None)
        (parts if is_part else buildings)[identity]=feature
    # The flattened building layer does not prove a complete association/part inventory.
    # Existing complete baseline parts are retained; uncertain updates remain conflicts.
    from dataclasses import replace
    for identity,p in parts.items():
        if p.parent_id not in buildings: issues.append((identity,'parent_not_in_complete_partition'))
    for identity,f in buildings.items():
        if any(p.parent_id==identity for p in parts.values()):
            buildings[identity]=replace(f,attrs=dict(f.attrs,has_parts=True))
    return Snapshot(tuple(bounds),tuple(buildings[k] for k in sorted(buildings)),
                    tuple(parts[k] for k in sorted(parts)),dict(version),tuple(sorted(issues)))
