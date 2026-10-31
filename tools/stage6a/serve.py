"""Explicit development companion; writes only beneath its marked kit workspace.

--smoke uses an isolated repository-local sandbox, not a game/user installation.
Normal execution binds loopback for a later user-operated, isolated game test.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import secrets
import sys
import threading
from types import SimpleNamespace


def no_external_network():
    connect,connect_ex,resolve=socket.socket.connect,socket.socket.connect_ex,socket.getaddrinfo
    def allowed(address):
        try: return ipaddress.ip_address(address[0]).is_loopback
        except (ValueError,TypeError): return False
    def guarded(self,address):
        if not allowed(address): raise RuntimeError('Stage 6A blocks every non-loopback socket')
        return connect(self,address)
    def guarded_ex(self,address):
        if not allowed(address): raise RuntimeError('Stage 6A blocks every non-loopback socket')
        return connect_ex(self,address)
    def guarded_resolve(host,*args,**kwargs):
        if not allowed((host,0)): raise RuntimeError('Stage 6A blocks DNS and external networking')
        return resolve(host,*args,**kwargs)
    socket.socket.connect=guarded; socket.socket.connect_ex=guarded_ex; socket.getaddrinfo=guarded_resolve


def workspace(path):
    root=Path(path).absolute()
    # Every component must be a real directory, not a junction redirecting a test profile.
    for parent in (root,*root.parents):
        if parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction()):
            raise RuntimeError('Workspace path cannot contain links or junctions')
    root=root.resolve(strict=True)
    marker=root/'STAGE6A_WORKSPACE.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('schema')!='stage6a-workspace-v1':
        raise RuntimeError('Use the workspace produced by prepare_stage6a.py')
    return root


def safe_child(root,path):
    if not path.resolve().is_relative_to(root): raise RuntimeError('Development path escaped the workspace')
    for part in (path,*path.parents):
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
            raise RuntimeError('Development paths cannot contain links or junctions')
    return path


async def run(args):
    root=workspace(args.workspace)
    source=safe_child(root,root/'source'/json.loads((root/'STAGE6A_WORKSPACE.json').read_text())['package_root'])
    sys.path.insert(0,str(root))
    sys.path.insert(0,str(source/'server'))
    # Existing render-reach knobs, not road geometry edits or raised activation quotas.
    # Default production distances exceed the current discovery quota in this fixture.
    if not args.default_ranges:
        os.environ.update(BNE_ROADS_SEND_M='384',BNE_ROADS_DROP_M='704',BNE_BLDFAR_SEND_M='384',
                          BNE_BLDFAR_DROP_M='896',BNE_SKY_SEND_M='768')
    else:
        for key in ('BNE_ROADS_SEND_M','BNE_ROADS_DROP_M','BNE_BLDFAR_SEND_M','BNE_BLDFAR_DROP_M','BNE_SKY_SEND_M'):
            os.environ.pop(key,None)
    import scenario
    import blockworker,buildinglive,buildingoverlaystore,earthd,fast,geodesy,mapcells,netfetch,osmbuildings,protocol,railways,revisit
    fast.require(fast.MIN_VERSION,'Stage 6A development build')
    no_external_network()
    # Ignore inherited endpoint/credential/debug settings in this process only.
    for key in ('BNGE_OSM_BUILDINGS','BNGE_OSM_ENDPOINT','BNGE_OSM_HEADERS','BNGE_BUILDING_OVERLAY_DIR','BNGE_SAMPLE','BNGE_TRACEMALLOC'):
        os.environ.pop(key,None)
    area=root/'smoke-sandboxes'/secrets.token_hex(6) if args.smoke else root/'run'
    safe_child(root,area)
    area.mkdir(parents=True,exist_ok=True)
    user=area/'profile'/'current'; cache=area/'cache'; logs=area/'logs'
    for p in (user,cache,logs):
        safe_child(root,p)
        p.mkdir(parents=True,exist_ok=True)
    event_path=logs/'events.jsonl'
    def event(kind,**fields):
        with event_path.open('a',encoding='utf-8') as f: f.write(json.dumps(dict(event=kind,**fields),sort_keys=True)+'\n')
    class NoHTTP:
        def request(self,*args,**kwargs): raise RuntimeError('No HTTP transport is installed in Stage 6A')
    net=netfetch.Fetcher(offline=True,transport=NoHTTP())
    fixture_fetcher=netfetch.Fetcher(offline=False,transport=NoHTTP())
    store=buildingoverlaystore.OverlayStore(cache/'building-overlays')
    md=scenario.SyntheticMapData(cache,fetcher=fixture_fetcher,overlay=store)
    worker_md=scenario.SyntheticMapData(cache,fetcher=net,overlay=store)
    dem=scenario.FlatDem(); md.dem=worker_md.dem=dem
    # This is the existing native worker state, configured for synthetic inputs.
    state=blockworker._State(str(cache),True,False,False,False,False)
    state.md=worker_md; state.dem=dem; state.fra=None
    blockworker._s=state; railways.COARSE_AT=dem.coarse_at; revisit.CACHE_DIR=str(cache)
    # Keep the first exercise in legacy frames; process/global-geotile startup is a later matrix.
    road_pool=ThreadPoolExecutor(2,thread_name_prefix='stage6a-roads')
    far_pool=ThreadPoolExecutor(1,thread_name_prefix='stage6a-far-buildings')
    server=earthd.Server(str(user),str(cache),tile_workers=2,imagery=None,near_visual='mesh',
        cells=32,block=512,horizon=False,far_format='cdae',mapdata=md,grading=False,
        road_widths=False,sidewalks=True,buildings=True,traffic=False,net=net,
        road_pool=road_pool,far_bld_pool=far_pool,radius=1500.)
    server.dem=dem; server.geo_tiles=False; server.global_far=False; server.update_check=False
    earthd.AHEAD_ON=False; earthd.ROUTE_AHEAD_ON=False
    # Ordinary processed-cell serialization, eagerly warmed for read-only previews.
    w,s,e,n=scenario.bounds(-3000,-3000,3000,3000)
    for key in mapcells.cells_for_bbox(s,w,n,e,max_cells=128):
        md.cell(key,layers=('buildings','building_parts','roads'))
    transport=scenario.FixtureTransport(event)
    server.configure_osm_buildings(osmbuildings.Config(enabled=True,endpoint=scenario.ENDPOINT),transport=transport)
    workflow=server._building_manual
    workflow.wall=workflow.client.wall=lambda:scenario.WALL
    live=server._building_live=buildinglive.Service(server,local_control=True)
    base_error=buildinglive._public_error
    def development_error(error):
        event('development_error',type=type(error).__name__,reason=str(error))
        logging.getLogger('stage6a').error('%s: %s',type(error).__name__,error)
        return base_error(error)
    buildinglive._public_error=development_error
    base_send=earthd.Connection.send
    baseline_roads={}
    # Log only whitelisted metadata, never messages containing write capabilities.
    def send(conn,msg):
        if msg.get('type')=='far_tile' and isinstance(msg.get('shape'),str):
            baseline_roads.setdefault(msg['shape'],dict(kind='ground_visual',key=msg['key']))
        if msg.get('type')=='roads_tile':
            for kind,slot in msg.get('slots',{}).items():
                if kind in ('road','walk','deck'):
                    baseline_roads.setdefault(slot[0],dict(kind=kind,key=msg['key']))
            if conn.active in server.anchors:
                a=server.anchors[conn.active]
                document=dict(roads=baseline_roads,anchor=dict(lat=a.lat,lon=a.lon),scenario=scenario.manifest())
                path=Path(server.user_dir)/'stage6a-baseline-roads.json'
                temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(document)); temp.replace(path)
        if msg.get('type') in ('building_activate','building_activate_batch','building_cancel'):
            event(msg['type'],transaction=msg.get('transaction'),revision=msg.get('revision'),
                  seq=msg.get('seq'),targets=len(msg.get('targets',())),part=msg.get('part'))
        elif msg.get('type')=='building_status':
            event('building_status',state=msg.get('state'),accepted_revision=msg.get('accepted_revision'),
                  activated_revision=msg.get('activated_revision'),error=(msg.get('error') or {}).get('type'))
        return base_send(conn,msg)
    earthd.Connection.send=send
    class DevelopmentConnection(earthd.Connection):
        def dispatch(self,msg):
            if msg['type']=='app_path':
                self.send(dict(type='app_paths',cacheDir=str(cache),cacheNext=str(cache),beammp='',
                    result=dict(what=msg.get('what','cache'),ok=False,text='Stage 6A paths are fixed inside the development kit.')))
                return
            if msg['type'] in ('open_page','update_now','server_list','random_place','place_previews','geocode','route'):
                self.status('This external action is unavailable in the synthetic Stage 6A companion.')
                return
            return super().dispatch(msg)
    tasks=set()
    async def connected(reader,writer):
        task=asyncio.current_task(); tasks.add(task)
        try: await DevelopmentConnection(server,reader,writer).run()
        finally: tasks.discard(task)
    listener=await asyncio.start_server(connected,'127.0.0.1',0 if args.smoke else args.port)
    port=listener.sockets[0].getsockname()[1]
    event('startup',synthetic=True,port=port,session=server.sid,profile=str(user),
          baseline=scenario.RELEASE,native_version=fast._ef.VERSION)
    print(f'STAGE 6A SYNTHETIC ONLY; listening 127.0.0.1:{port}; profile={user}',flush=True)
    print('No public HTTP/DNS permitted; no actual BeamNG test is claimed.',flush=True)
    try:
        if args.smoke:
            await smoke(server,port,transport,scenario,protocol,root)
        else:
            async with listener: await listener.serve_forever()
    finally:
        listener.close(); await listener.wait_closed()
        for task in list(tasks): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await live.close()
        executors={v for v in vars(server).values() if isinstance(v,ThreadPoolExecutor)}|{road_pool,far_pool}
        await asyncio.to_thread(lambda:[pool.shutdown(wait=True,cancel_futures=True) for pool in executors])
        md.close(); worker_md.close(); net.close(); fixture_fetcher.close()
        earthd.Connection.send=base_send
        buildinglive._public_error=base_error


async def smoke(server,port,transport,scenario,protocol,root):
    reader,writer=await asyncio.open_connection('127.0.0.1',port)
    poses=None
    async def send(message): writer.write(protocol.encode(message)); await writer.drain()
    async def until(kind,predicate=lambda m:True):
        async with asyncio.timeout(90):
            while True:
                message=await protocol.read_message(reader)
                if message is None: raise RuntimeError('Companion closed during smoke test')
                if message['type']==kind and predicate(message): return message
    try:
        await send(dict(type='hello',protocol=4,format=4,modVersion='stage6a-smoke',buildingActivation=1,
                        buildingBatches=1,buildingControl=1,buildingMultiplayer=False))
        hello=await until('hello')
        if hello.get('buildingControl')!=1: raise RuntimeError('Local controls unavailable')
        # Exercise the actual wire/startup/flat terrain path before manual data work.
        await send(dict(type='prepare_anchor',requestId=1,**scenario.manifest()['viewpoint']))
        anchor=await until('anchor_ready')
        await send(dict(type='commit_anchor',anchorId=anchor['anchorId']))
        pose=dict(type='pose',anchorId=anchor['anchorId'],x=anchor.get('px') or 0,y=anchor.get('py') or 0,
                  z=2,vx=0,vy=0,lat=scenario.manifest()['viewpoint']['lat'],lon=scenario.manifest()['viewpoint']['lon'],speed=0)
        async def keep_posing():
            while True:
                await send(pose); await asyncio.sleep(.25)
        poses=asyncio.create_task(keep_posing())
        await until('roads_tile') # actual baseline streaming, not a seeded tracker or fake engine ack
        conn=next(iter(server.connections))
        async with asyncio.timeout(60):
            while not conn.roads.car.issubset(conn.roads.sent|conn.roads.empty) or conn.roads.futs:
                await asyncio.sleep(.1)
        async def command(op,identity,**fields):
            await send(dict(type='building_control',op=op,id=identity,sessionId=hello['sessionId'],
                epoch=hello['buildingEpoch'],authorization=hello['buildingControlToken'],**fields))
        await command('preview',1,bounds=list(scenario.bounds()))
        review=await until('building_status',lambda m:m['state'] in ('awaiting_acceptance','failed'))
        if review['state']=='failed': raise RuntimeError(review.get('error'))
        expected=scenario.manifest()['expected_preview']
        if review['preview']['counts']!={k:expected[k] for k in ('addition','replacement','unchanged','conflict')}:
            raise RuntimeError('Unexpected preview counts: '+str(review['preview']['counts']))
        await asyncio.sleep(.3)
        await command('accept',2,preview_token=review['preview']['token'],confirm=True)
        terminal=await until('building_status',lambda m:m['state'] in ('activation_scheduled','accepted','failed','superseded'))
        if terminal['state']=='accepted':
            terminal=await until('building_status',lambda m:m['state'] in ('activation_scheduled','failed','superseded'))
        if terminal['state']!='activation_scheduled':
            failure=dict(synthetic=True,status=terminal['state'],error=terminal.get('error'),engine_tested=False,
                         fixture_requests=transport.calls,accepted_revision=terminal.get('accepted_revision'))
            (root/'smoke-failure.json').write_text(json.dumps(failure,indent=2)+'\n')
            raise RuntimeError(failure)
        catalog=await asyncio.to_thread(server._building_refresh.outputs)
        meshes=[str(server._building_refresh.io.objects/row['generation']/name) for row in catalog['entries'].values()
                for name in row['files'] if name.endswith('.cdae') and 'bld' in name]
        if not meshes or not all(Path(p).is_file() for p in meshes):
            raise RuntimeError('No actual building mesh reached the prepared catalog')
        result=dict(synthetic=True,hello=True,anchor_ready=True,preview=review['preview']['counts'],
            accepted_revision=terminal['accepted_revision'],status=terminal['state'],fixture_requests=transport.calls,
            game_activated=False,engine_tested=False,baseline_streamed=True,prepared_building_mesh_files=len(meshes),
            prepared_targets=len(catalog['entries']),
            terrain_file_present=(Path(server.user_dir)/anchor['terrain']['heightmap'].lstrip('/')).is_file())
        (root/'smoke-results.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result),flush=True)
    finally:
        if poses is not None:
            poses.cancel(); await asyncio.gather(poses,return_exceptions=True)
        writer.close(); await writer.wait_closed()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--synthetic-only',action='store_true',required=True)
    parser.add_argument('--port',type=int,default=47623)
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--default-ranges',action='store_true',help='Reproduce the known unmodified production-distance discovery quota blocker')
    args=parser.parse_args()
    if not 1024<=args.port<=65535: parser.error('Choose a non-privileged loopback port')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try: asyncio.run(run(args))
    except KeyboardInterrupt: pass


if __name__=='__main__': main()
