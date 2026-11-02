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
import struct
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
        self.lock = threading.RLock()
        self.failure = None
        self.admit = None
        self.failed = None

    def get(self, url, *, kind='demand', headers=None):
        # The Parquet planner may prefetch ranges on multiple threads. Serialize
        # admission THROUGH body completion, not just byte reservations, so the
        # persistent ledger and endpoint really see one building request at a time.
        with self.lock:
            if self.failure is not None:raise self.failure
            try:return self._get(url,kind=kind,headers=headers)
            except BaseException as error:
                self.failure=error
                raise

    def _get(self, url, *, kind='demand', headers=None):
        m = re.fullmatch(r'bytes=(\d+)-(\d+)', (headers or {}).get('Range', ''))
        amount = int(m[2])-int(m[1])+1 if m else MAX_BYTES+1
        with self.lock:
            if self.used + amount > MAX_BYTES: raise ResourceLimit('Layercake refresh network budget')
            self.used += amount  # reserve before the request; failures count too
        if self.admit: self.admit(amount)
        with self.fetcher.session(url, kind=kind) as session:
            status, rh, body = self.transport.request(url, dict(session.headers, **(headers or {})),
                                                    self.fetcher.policy(urlsplit(url).hostname).timeout)
            session.status, session.nbytes = status, len(body)
            if status != 206:
                if self.failed: self.failed(status, rh)
                raise osm.HTTPFailure(status, rh)
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
    def __init__(self, raw_dir, fetcher, *, transport=None, tolerant=False, raw_budget=256*1024*1024):
        self.ranges = RangeFetcher(fetcher, transport)
        super().__init__(str(raw_dir), self.ranges, URL)
        self.selected = None
        self.tolerant = tolerant
        self.raw_budget = raw_budget

    def close(self): self._reader.close()

    def attach(self, version):
        """Reuse a session-selected source without a freshness request."""
        if self.selected is not None: raise ValueError('reader already selected')
        from buildingprimary import validate_version
        validate_version(version)
        self.selected=dict(version)
        directory=Path(self.raw_dir)/'osm-buildings'/hashlib.sha256(version['etag'].encode()).hexdigest()
        self._remote=(version['etag'],rangeread.RemoteFile(URL,version['size'],str(directory),self.ranges,if_match=version['etag']))
        return dict(self.selected)

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
                if count>32768: raise ResourceLimit('raw building cache entry limit')
                if p.is_file(): size+=p.stat().st_size
                if size+MAX_BYTES>self.raw_budget: raise ResourceLimit('raw building cache capacity; no automatic cleanup')
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
            expressions=[]
            for c in columns:
                # Bound variable-width values in SQL BEFORE fetchall allocates
                # Python objects. Oversized tags are explicitly deferred, never
                # silently accepted with shortened attributes.
                value=f'CAST("{c}" AS VARCHAR)' if c=='timestamp' else f'"{c}"'
                limit=4096 if c in TAGS else 128
                expressions.append(f'CASE WHEN length(CAST("{c}" AS VARCHAR)) <= {limit} THEN {value} END AS "{c}"')
            too_big=' OR '.join(f'length(CAST("{c}" AS VARCHAR)) > 4096' for c in columns if c in TAGS) or 'false'
            expressions.append(f'({too_big}) AS _oversized_attributes')
            expressions.append('(octet_length(geometry) > 131072) AS _oversized_geometry')
            geometry='CASE WHEN octet_length(geometry) <= 131072 THEN geometry END AS geometry'
            rows = con.execute(f'''SELECT {','.join(expressions)}, {geometry}
                FROM read_parquet({uri}, file_row_number=true) WHERE ({only}) AND {box}
                LIMIT {MAX_FEATURES+1}''').fetchall()
            if len(rows)>MAX_FEATURES: raise ResourceLimit('building partition feature budget exceeded')
            return normalize([dict(zip(columns+['_oversized_attributes','_oversized_geometry','geometry'],r)) for r in rows],bounds,self.selected,tolerant=self.tolerant)
        except Exception:
            if errors: raise errors[0]
            raise
        finally: con.close()


def normalize(rows, bounds, version, *, tolerant=False):
    """A malformed feature can retain its fallback without rejecting a city.

    Structural duplicate identities still reject the partition. Full geometry is
    never flattened; the regional preparer reports and defers unsupported groups.
    """
    if not tolerant: return _normalize_rows(rows,bounds,version)
    buildings,parts,issues,seen=[],[],[],set()
    for row in rows:
        identity=bo.normalize_osm_id(row.get('id'))
        if identity is None and row.get('osm_type',row.get('type')) in ('way','relation'):
            identity=bo.normalize_osm_id(str(row.get('osm_type',row.get('type')))+'/'+str(row.get('id')))
        if identity is None: raise ValueError('missing typed OSM identity; partition cannot be verified')
        if identity in seen: raise ValueError('duplicate OSM identity in partition')
        seen.add(identity)
        issue_start=len(issues)
        try:
            result=_normalize_rows([row],bounds,version)
            buildings.extend(result.buildings);parts.extend(result.parts)
            issues.extend(i for i in result.issues if i[1]!='parent_not_in_complete_partition')
        except (ValueError,TypeError,KeyError,IndexError,OverflowError,ResourceLimit) as error:
            issues.append((identity,'malformed_record:'+type(error).__name__))
        except struct.error:
            issues.append((identity,'malformed_wkb'))
        parent=bo.normalize_osm_id(row.get('building_id',row.get('parent_id')))
        if parent and str(row.get('building:part','no')).strip().lower() not in ('no','','false','0') and len(issues)>issue_start:
            issues.append((parent,'incomplete_part_geometry'))
    parents={f.feature_id for f in buildings}
    issues.extend((p.feature_id,'parent_not_in_complete_partition') for p in parts if p.parent_id not in parents)
    return Snapshot(tuple(bounds),tuple(buildings),tuple(parts),dict(version),tuple(sorted(set(issues))))


def _normalize_rows(rows, bounds, version):
    """Normalize the actual flattened Layercake schema; retain full WKB topology."""
    buildings, parts, issues, identities = {}, {}, [], set()
    for row in rows:
        identity = bo.normalize_osm_id(row.get('id'))
        if identity is None and row.get('osm_type',row.get('type')) in ('way','relation'):
            identity = bo.normalize_osm_id(str(row.get('osm_type',row.get('type')))+'/'+str(row['id']))
        if identity is None: raise ValueError('missing typed OSM building identity')
        if identity in identities: raise ValueError('duplicate OSM identity in complete source')
        identities.add(identity)
        if row.get('_oversized_attributes'): issues.append((identity,'oversized_attributes'))
        if row.get('_oversized_geometry'):
            issues.append((identity,'geometry_byte_limit'));continue
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
        is_part = tags.get('building:part','no').strip().lower() not in ('no','', 'false','0')
        if not is_part and tags.get('building','no').strip().lower() in ('no','', 'false','0'):
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
