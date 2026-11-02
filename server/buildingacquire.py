"""Killable data-only acquisition; never owns or changes engine resources.

The road worker supervises exactly its own Python child. Native DuckDB, DNS and
socket stalls cannot defeat the generation-read deadline. A separate OS lease
serializes acquisition/network work without holding the world publication lock.
"""
from dataclasses import replace
import json
import multiprocessing as mp
import os
import threading
import time

import buildingoverlaystore as fs

MAX_REPLY=96*1024*1024


class AcquisitionError(OSError):pass


def _wire(result):
    snapshot,source,report=result
    return dict(release=snapshot.baseline_release,entries=[fs._entry_wire(e) for e in snapshot.entries],
                source=source,report=report)


def _decode(value):
    from buildingworld import _empty
    if set(value)!={'release','entries','source','report'} or len(value['entries'])>50000:
        raise AcquisitionError('invalid acquisition result')
    entries=tuple(fs.Entry(fs._feature(e['feature']),tuple(fs._feature(p) for p in e['parts']),
                          e['baseline_digest'],fs._bounds(e['bounds']),e['source_timestamp']) for e in value['entries'])
    snapshot=replace(_empty(value['release']),entries=entries,affected_bounds=fs._union([e.bounds for e in entries]))
    return snapshot,value['source'],value['report']


def _default(request,emit):
    import mapdata,netfetch
    from buildingworld import Provider
    fetcher=netfetch.Fetcher(offline=request['offline'],stats_dir=os.path.join(request['cache'],'netstats'))
    md=mapdata.MapData(request['cache'],fetcher=fetcher,release=request['release'],building_overlays=False)
    try:
        provider=Provider(md,request['session'])
        if not provider.claim_valid(request['key'],request['token']):raise AcquisitionError('acquisition claim fenced')
        state=provider._state()
        return provider._acquire(request['key'],state,{e.feature.feature_id:e for e in request['pinned']},progress=emit)
    finally:md.close();fetcher.close()


def _worker(connection,request,deadline,task):
    import buildingworld as world
    parent=mp.parent_process();finished=threading.Event()
    def watch():
        while not finished.wait(.05):
            if time.monotonic()>=deadline or parent is not None and not parent.is_alive():
                os._exit(124)  # this data-only child, never any unrelated process
    threading.Thread(target=watch,daemon=True,name='osm-query-watchdog').start()
    def send(value):
        body=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
        if len(body)>MAX_REPLY:raise fs.ResourceLimit('acquisition reply byte budget')
        connection.send_bytes(body)
    try:
        store=world._store(request['cache'])
        # The child's own lease also covers the brief interval after a parent is
        # killed and before this child's parent watchdog notices it.
        with world.lease_store(store,'query-network',max(.01,deadline-time.monotonic()))._locked():
            if time.monotonic()>=deadline:raise TimeoutError('acquisition deadline before network')
            emit=lambda phase:send(dict(kind='phase',phase=phase))
            result=(task or _default)(request,emit)
            if time.monotonic()>=deadline:raise TimeoutError('acquisition deadline before result')
            send(dict(kind='result',value=_wire(result)))
    except BaseException as error:
        try:send(dict(kind='error',error=type(error).__name__+': '+str(error)[:512]))
        except (OSError,ValueError):pass
    finally:
        finished.set();connection.close()


class Runner:
    def __init__(self,task=None):self.task=task

    def run(self,request,deadline,progress=lambda value:None):
        if time.monotonic()>=deadline:raise TimeoutError('acquisition read budget exhausted')
        ctx=mp.get_context('spawn');read,write=ctx.Pipe(duplex=False)
        process=ctx.Process(target=_worker,args=(write,request,deadline,self.task),name='osm-building-acquire')
        phase={'phase':'starting'}
        try:process.start()
        except BaseException:
            read.close();write.close();process.close()
            raise
        write.close()
        def stop_owned():
            try:
                if process.is_alive():process.terminate()
            except (OSError,ValueError):pass
        timer=threading.Timer(max(.001,deadline-time.monotonic()),stop_owned)
        timer.daemon=True;timer.start()
        try:
            while time.monotonic()<deadline:
                if not read.poll(min(.05,max(0,deadline-time.monotonic()))):
                    if not process.is_alive():break
                    continue
                try:message=json.loads(read.recv_bytes(MAX_REPLY))
                except (EOFError,OSError):break
                if message.get('kind')=='phase':phase=message['phase'];progress(phase)
                elif message.get('kind')=='error':raise AcquisitionError(message['error'])
                elif message.get('kind')=='result':
                    if time.monotonic()>=deadline:break
                    return _decode(message['value'])
                else:raise AcquisitionError('invalid acquisition message')
            raise TimeoutError('OSM acquisition deadline/worker exit; last phase='+str(phase)[:512])
        finally:
            timer.cancel();read.close()
            process.join(timeout=.2)
            if process.is_alive():
                stop_owned();process.join(timeout=1)
            process.close()
