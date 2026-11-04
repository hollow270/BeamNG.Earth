"""Bounded, reusable source-row indexes. These are not accepted building choices.

A Parquet row group is decoded once per source version. SQLite's native R*Tree
then answers small geographical reads without reopening the planet Parquet file.
Full WKB/tags are kept unchanged; the existing normalizer still validates every
accepted cell. Nothing here owns meshes, collision or session revisions.
"""
from contextlib import closing
from collections import OrderedDict
import hashlib,json,math,os,re,sqlite3,time,uuid,threading,weakref
from pathlib import Path

import buildingoverlaystore as fs

FORMAT=2
MAX_GROUP_ROWS=1_000_000
MAX_FILE=128*1024**2
MAX_PAYLOAD=128*1024
BUILD_SECONDS=8.
BATCH=64
_writers=weakref.WeakValueDictionary()
_writers_guard=threading.Lock()


class Unavailable(Exception):
    """Optional acceleration cannot fit; use the original bounded source query."""


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as f:
        while chunk:=f.read(1024**2):value.update(chunk)
    return value.hexdigest()


class Index:
    def __init__(self,reader):
        self.reader=reader
        version=reader.selected
        self.raw_root=Path(reader.raw_dir).absolute()/'osm-buildings'
        self.index_root=reader.index_dir or self.raw_root
        self.root=self.index_root/hashlib.sha256(version['etag'].encode()).hexdigest()/'rows-v2'
        for p in reversed((self.root,*self.root.parents)):fs.OverlayStore._no_link(p)
        self.root.mkdir(parents=True,exist_ok=True)
        self.io=object.__new__(fs.OverlayStore)
        self.io.directory=self.index_root.parent;self.io.root=self.root
        self.io.limits=fs.Limits(max_file_bytes=16*1024**2);self.io.lock_timeout=15.
        self.checked={};self.connections=OrderedDict();self.groups=None;self.schema=None;self.names=None
        with _writers_guard:
            self.local=_writers.get(str(self.root))
            if self.local is None:self.local=threading.RLock();_writers[str(self.root)]=self.local

    def close(self):
        for db in self.connections.values():db.close()
        self.connections.clear()

    def connection(self,path):
        db=self.connections.get(path)
        if db is not None:self.connections.move_to_end(path);return db
        db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,check_same_thread=False)
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA cache_size=-2048')
        self.connections[path]=db
        while len(self.connections)>2:self.connections.popitem(last=False)[1].close()
        return db

    def catalog(self):
        if self.groups is not None:return self.groups
        path=self.root/'catalog.json'
        if not path.exists():
            with self.local,self.io._locked():
                if not path.exists():
                    con,proto,_=self.reader._connection();uri=f"'{proto}://osm/buildings.parquet'"
                    groups=self.reader._groups(con,uri,self.reader._remote[1],'demand')
                    schema=sorted(r[0] for r in con.execute(f'DESCRIBE SELECT * FROM read_parquet({uri})').fetchall())
                    self._room(16*1024**2)
                    self.io._atomic_write(path,self.io._document(dict(format=FORMAT,source=self.reader.selected,
                        schema=schema,groups={str(k):v for k,v in groups.items()})))
        value=self.io._read(path)
        if set(value)!={'format','source','schema','groups'} or value['format']!=FORMAT or value['source']!=self.reader.selected:
            raise fs.CorruptStorage('source index version mismatch')
        if not isinstance(value['schema'],list) or len(value['schema'])>128 or any(not isinstance(s,str) or len(s)>128 for s in value['schema']):
            raise fs.CorruptStorage('source index schema')
        required={'id','geometry','bbox','building','version','timestamp'}
        if not required<=set(value['schema']):raise fs.CorruptStorage('source index missing columns')
        if not isinstance(value['groups'],dict) or len(value['groups'])>65536:raise fs.CorruptStorage('source index group capacity')
        groups={}
        for key,g in value['groups'].items():
            if not key.isdecimal() or not isinstance(g,dict) or set(g)!={'first','n','bbox','cols'} or type(g['first']) is not int or g['first']<0 or type(g['n']) is not int or g['n']<=0:
                raise fs.CorruptStorage('source index invalid group')
            if not isinstance(g['bbox'],dict) or not isinstance(g['cols'],dict) or len(g['cols'])>128:
                raise fs.CorruptStorage('source index group schema')
            for col,ranges in g['cols'].items():
                if not isinstance(col,str) or not isinstance(ranges,list) or len(ranges)>128:raise fs.CorruptStorage('source index column')
                for pair in ranges:
                    if not isinstance(pair,list) or len(pair)!=2 or any(type(v) is not int for v in pair) or not 0<=pair[0]<pair[1]<=self.reader.selected['size']:
                        raise fs.CorruptStorage('source index byte range')
            groups[int(key)]=g
        self.schema=set(value['schema']);self.groups=groups
        self.names,_=self.reader._projection(self.schema)
        return groups

    def _room(self,reserve):
        # Shares the EXISTING raw-source budget, including range blocks and any
        # interrupted temporary files. A cache miss never justifies an overrun.
        used=count=0;roots=[self.raw_root]
        if not self.index_root.is_relative_to(self.raw_root):roots.append(self.index_root)
        for root in roots:
            for p in root.rglob('*'):
                fs.OverlayStore._no_link(p);count+=1
                if count>32768:raise Unavailable('source index file budget')
                if p.is_file():used+=p.stat().st_size
        available=self.reader.raw_budget-used
        if available<reserve:raise Unavailable('source index raw-byte budget')
        return available

    def _valid(self,group):
        mark=self.root/f'{group}.json'
        self.io._safe(mark)
        try:ms=mark.stat()
        except FileNotFoundError:return None
        marker=(ms.st_ino,ms.st_size,ms.st_mtime_ns)
        prior=self.checked.get(group)
        if prior is not None and prior[0]==marker:
            path=self.io._safe(self.root/prior[2]);stat=path.stat()
            if (stat.st_ino,stat.st_size,stat.st_mtime_ns)==prior[1]:return path
        stamp=self.io._read(mark)
        if set(stamp)!={'format','source','group','rows','names','bytes','sha256','file'} or stamp['format']!=FORMAT or stamp['source']!=self.reader.selected or stamp['group']!=group or stamp['rows']!=self.groups[group]['n'] or stamp['names']!=self.names:
            raise fs.CorruptStorage('source row index identity')
        if not isinstance(stamp['file'],str) or not re.fullmatch(str(group)+r'\.[0-9a-f]{32}\.sqlite',stamp['file']):
            raise fs.CorruptStorage('source row index filename')
        if type(stamp['bytes']) is not int or not 0<stamp['bytes']<=MAX_FILE or not isinstance(stamp['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',stamp['sha256']):
            raise fs.CorruptStorage('source row index extent')
        path=self.io._safe(self.root/stamp['file']);stat=path.stat()
        identity=(stat.st_ino,stat.st_size,stat.st_mtime_ns)
        if stat.st_size!=stamp['bytes']:raise fs.CorruptStorage('source row index size')
        if digest(path)!=stamp['sha256']:raise fs.CorruptStorage('source row index checksum')
        self.checked[group]=(marker,identity,stamp['file'])
        return path

    def ensure(self,group):
        if self._valid(group) is not None:return
        g=self.groups[group]
        if g['n']>MAX_GROUP_ROWS:raise Unavailable('source row group capacity')
        with self.local,self.io._locked():
            if self._valid(group) is not None:return
            work=self.root/f'{group}.sqlite.work';path=self.root/f'{group}.{uuid.uuid4().hex}.sqlite'
            # This OS lock excludes every index writer. These deterministic
            # work files cannot have been returned by _valid(). A finished DB
            # with a lost marker might have readers: NEVER reclaim that file.
            # A replacement gets a unique name and shares the same byte budget.
            for p in (work,work.with_name(work.name+'-journal')):
                self.io._safe(p)
                if p.exists():p.unlink()
            con,proto,errors=self.reader._connection();uri=f"'{proto}://osm/buildings.parquet'"
            wanted=set(self.names)|{'bbox'}
            self.reader._reader._fetch({'osm/buildings.parquet':self.reader._remote[1]},
                {'osm/buildings.parquet':[r for c,rs in g['cols'].items() if c in wanted for r in rs]},'demand')
            remaining=max(0,96*1024**2-self.reader.ranges.used)
            available=min(MAX_FILE,self._room(1024**2+remaining)-remaining-1024**2)
            if available<65536:raise Unavailable('source index journal reservation')
            end=time.monotonic()+BUILD_SECONDS
            _,projection=self.reader._projection(self.schema)
            db=None
            try:
                db=sqlite3.connect(work)
                db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA trusted_schema=OFF');db.execute('PRAGMA cache_size=-16384')
                db.execute('PRAGMA temp_store=MEMORY')
                page_size=db.execute('PRAGMA page_size').fetchone()[0]
                db.execute(f'PRAGMA max_page_count={available//page_size}')
                db.set_progress_handler(lambda:int(time.monotonic()>=end),10000)
                db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,x0 REAL,y0 REAL,x1 REAL,y1 REAL,payload BLOB,geometry BLOB)')
                db.execute('CREATE VIRTUAL TABLE boxes USING rtree(id,x0,x1,y0,y1)')
                cursor=con.execute(f'''SELECT {projection}, bbox, file_row_number FROM read_parquet({uri},file_row_number=true)
                    WHERE file_row_number BETWEEN {g['first']} AND {g['first']+g['n']-1} LIMIT {g['n']+1}''')
                self.reader.diagnostics['remote_scans']+=1
                count=0
                while rows:=cursor.fetchmany(BATCH):
                    if time.monotonic()>=end:raise Unavailable('source index build time')
                    entries=[];boxes=[]
                    for row in rows:
                        values=row[:len(self.names)];bounds=row[-2];number=row[-1]
                        coords=tuple(bounds.get(k) for k in ('xmin','ymin','xmax','ymax')) if isinstance(bounds,dict) else ()
                        if len(coords)!=4 or any(type(v) not in (float,int) or not math.isfinite(v) for v in coords) or coords[0]>coords[2] or coords[1]>coords[3]:
                            raise Unavailable('source index uncertain bbox')
                        body=json.dumps(values[:-1],separators=(',',':'),allow_nan=False).encode()
                        if len(body)>MAX_PAYLOAD:raise Unavailable('source index row bytes')
                        entries.append((number,*coords,body,values[-1]));boxes.append((number,coords[0],coords[2],coords[1],coords[3]))
                    db.executemany('INSERT INTO records VALUES (?,?,?,?,?,?,?)',entries)
                    db.executemany('INSERT INTO boxes VALUES (?,?,?,?,?)',boxes)
                    count+=len(rows)
                    if count>g['n']:raise fs.CorruptStorage('source index excess rows')
                if count!=g['n']:raise fs.CorruptStorage('source index incomplete group')
                db.commit();db.close();db=None
                with work.open('r+b') as f:os.fsync(f.fileno())
                stamp=dict(format=FORMAT,source=self.reader.selected,group=group,rows=count,names=self.names,
                           bytes=work.stat().st_size,sha256=digest(work),file=path.name)
                os.replace(work,path)
                self.io._atomic_write(self.root/f'{group}.json',self.io._document(stamp))
                self.reader.diagnostics['indexed_groups_built']=self.reader.diagnostics.get('indexed_groups_built',0)+1
            except sqlite3.OperationalError as error:
                if any(s in str(error) for s in ('full','interrupt','no such module: rtree')):
                    raise Unavailable('source index bounded build or unavailable local index') from error
                raise
            finally:
                if db is not None:db.close()
                for p in (work,work.with_name(work.name+'-journal')):
                    self.io._safe(p)
                    if p.exists():p.unlink()
        self._valid(group)

    @staticmethod
    def _sql(count=False):
        fields='1' if count else 'r.payload,r.geometry'
        return f'''SELECT {fields} FROM boxes b JOIN records r ON r.id=b.id
            WHERE b.x0<=? AND b.x1>=? AND b.y0<=? AND b.y1>=?
              AND r.x0<=? AND r.x1>=? AND r.y0<=? AND r.y1>=? ORDER BY r.id LIMIT ?'''

    def read(self,groups,bounds,limit,*,count=False):
        if type(limit) is not int or not 0<limit<=50001:raise ValueError('invalid source index query limit')
        w,s,e,n=bounds;out=[];total=0
        for group in groups:
            path=self._valid(group)
            if path is None:raise fs.CorruptStorage('source index missing completed group')
            try:
                db=self.connection(path)
                try:
                    end=time.monotonic()+BUILD_SECONDS
                    db.set_progress_handler(lambda:int(time.monotonic()>=end),10000)
                    args=(e,w,n,s,e,w,n,s,limit-total)
                    if count:
                        total+=db.execute('SELECT count(*) FROM ('+self._sql(True)+')',args).fetchone()[0]
                    else:
                        for payload,geometry in db.execute(self._sql(),args):
                            if not isinstance(payload,bytes) or len(payload)>MAX_PAYLOAD:raise fs.CorruptStorage('source index row extent')
                            values=json.loads(payload)
                            if not isinstance(values,list) or len(values)!=len(self.names)-1:raise fs.CorruptStorage('source index row schema')
                            out.append(dict(zip(self.names,(*values,geometry))));total+=1
                finally:db.set_progress_handler(None,0)
            except sqlite3.DatabaseError as error:raise fs.CorruptStorage('invalid source row index') from error
            if total>=limit:break
        return total if count else out

    def prepare(self,bounds):
        groups=self.catalog();selected=self.reader._candidates(groups,bounds)
        for group in selected:self.ensure(group)
        count=self.read(selected,bounds,50001,count=True)
        if count>50000:
            raise fs.ResourceLimit('source staging feature capacity')
        self.reader.diagnostics.update(row_groups=len(groups),candidate_groups=selected[:64],
            candidate_rows=sum(groups[g]['n'] for g in selected),indexed_groups_reused=len(selected),
            staged_rows=count)
        return selected
